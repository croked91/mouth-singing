"""Remote sync operations for the bootstrap pipeline.

Manages SSH/SCP operations between the local processing machine and the
remote server that holds the MP3 library and receives output files.

Uses an SSH ControlMaster socket so that all operations share a single TCP
connection, eliminating per-command handshake overhead (~0.5s × many commands
× thousands of tracks would otherwise add hours of pure overhead).

Typical usage::

    syncer = RemoteSyncer(
        host="root@130.49.170.186",
        remote_mp3_dir="/root/mp3_library",
        remote_output_dir="/root/bootstrap_output",
        remote_db_path="/root/bootstrap_output/karaoke.db",
    )
    syncer.start()
    try:
        syncer.ensure_remote_setup()
        for filename in syncer.list_remote_mp3s():
            local_path = syncer.pull_mp3(filename, local_dir)
            # ... process locally ...
            syncer.push_file(instrumental_path, remote_dir)
            syncer.insert_remote_track(track_data)
            syncer.delete_remote_file(remote_mp3_path)
    finally:
        syncer.stop()
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Directory on the local machine where the SSH ControlMaster socket is placed.
_CONTROL_SOCKET_DIR = Path("/tmp/bootstrap_ssh")

# Subdirectory inside the remote MP3 dir used for atomic file claiming.
# Workers ``mv`` an MP3 here before processing to prevent other workers from
# picking the same file.  See ``claim_mp3()`` / ``unclaim_mp3()``.
_CLAIM_SUBDIR = ".processing"

# The SQL schema used to initialise the remote SQLite database. Must stay in
# sync with BootstrapRunner._ensure_db_schema in bootstrap_runner.py.
_REMOTE_DB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    duration_sec INTEGER,
    mp3_path TEXT,
    instrumental_path TEXT,
    clip_path TEXT,
    lyrics_text TEXT,
    syllable_timings TEXT,
    language TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    play_count INTEGER NOT NULL DEFAULT 0,
    qdrant_synced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);
"""

# The INSERT statement used in insert_remote_track. Named placeholders map
# directly onto the dict keys, so field order is explicit and safe.
_REMOTE_INSERT_SQL = """
INSERT OR REPLACE INTO tracks (
    id, artist, title, duration_sec, mp3_path, instrumental_path,
    clip_path, lyrics_text, syllable_timings, language, source,
    status, error_message, play_count, qdrant_synced,
    created_at, updated_at
) VALUES (
    :id, :artist, :title, :duration_sec, :mp3_path, :instrumental_path,
    :clip_path, :lyrics_text, :syllable_timings, :language, :source,
    :status, :error_message, :play_count, :qdrant_synced,
    :created_at, :updated_at
)
"""


class RemoteSyncError(Exception):
    """Raised when an SSH/SCP operation fails.

    Attributes:
        operation: Short description of what was attempted.
        returncode: The process exit code.
        stderr: Standard error output from the failed process.
    """

    def __init__(self, operation: str, returncode: int, stderr: str) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"Remote operation failed [{operation}] "
            f"exit={returncode}: {stderr.strip()[:200]}"
        )


class RemoteSyncer:
    """Manages all SSH/SCP operations between local machine and remote server.

    All SSH calls share a single TCP connection via an SSH ControlMaster socket,
    which avoids per-command handshake latency.

    Args:
        host: SSH host string, e.g. "root@130.49.170.186".
        remote_mp3_dir: Directory on the remote server containing source MP3s.
        remote_output_dir: Directory on the remote server for output files.
        remote_db_path: Path to the SQLite database on the remote server.
    """

    def __init__(
        self,
        host: str,
        remote_mp3_dir: str,
        remote_output_dir: str,
        remote_db_path: str,
    ) -> None:
        self._host = host
        self._remote_mp3_dir = remote_mp3_dir
        self._remote_output_dir = remote_output_dir
        self._remote_db_path = remote_db_path
        self._started = False

    @property
    def _control_path(self) -> str:
        """Filesystem path of the SSH ControlMaster socket for this host."""
        # Sanitise the host string so it can be part of a filename.
        safe_host = self._host.replace("@", "-").replace(".", "-").replace(":", "-")
        return str(_CONTROL_SOCKET_DIR / f"ctrl-{safe_host}")

    def start(self) -> None:
        """Establish the SSH ControlMaster connection.

        Starts a background SSH process in master mode. All subsequent _ssh()
        calls will reuse this connection rather than doing a new handshake.

        Raises:
            RemoteSyncError: If the ControlMaster process fails to start.
        """
        _CONTROL_SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        # Set restrictive permissions so other users cannot access the socket.
        _CONTROL_SOCKET_DIR.chmod(0o700)

        cmd = [
            "ssh",
            "-MNf",  # Master mode, no command, background
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=600",
            "-o", "ServerAliveInterval=30",
            "-o", "StrictHostKeyChecking=accept-new",
            self._host,
        ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RemoteSyncError(
                "ControlMaster start",
                result.returncode,
                result.stderr.decode(errors="replace"),
            )

        self._started = True
        logger.info(
            "remote_syncer.connected",
            host=self._host,
            control_path=self._control_path,
        )

    def stop(self) -> None:
        """Tear down the SSH ControlMaster connection.

        Errors are suppressed — if the socket is already gone (e.g. the server
        closed the connection), there is nothing useful to do.
        """
        if not self._started:
            return

        cmd = [
            "ssh",
            "-O", "exit",
            "-o", f"ControlPath={self._control_path}",
            self._host,
        ]
        subprocess.run(cmd, capture_output=True)  # Ignore errors intentionally.
        self._started = False
        logger.info("remote_syncer.disconnected", host=self._host)

    def _ssh(
        self,
        remote_cmd: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a shell command on the remote host via the ControlMaster socket.

        Args:
            remote_cmd: Shell command string to execute on the remote host.
            input_bytes: Optional bytes to send on stdin of the remote command.
            check: If True, raise RemoteSyncError on non-zero exit code.

        Returns:
            The completed process object with stdout and stderr.

        Raises:
            RemoteSyncError: If the command fails and check=True.
        """
        cmd = [
            "ssh",
            "-o", "ControlMaster=no",
            "-o", f"ControlPath={self._control_path}",
            "-o", "StrictHostKeyChecking=accept-new",
            self._host,
            remote_cmd,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            input=input_bytes,
        )

        if check and result.returncode != 0:
            raise RemoteSyncError(
                remote_cmd[:80],
                result.returncode,
                result.stderr.decode(errors="replace"),
            )

        return result

    def list_remote_mp3s(self) -> list[str]:
        """List MP3 filenames available in the remote MP3 directory.

        Returns:
            List of MP3 filenames (basenames only, not full paths). The list
            is sorted for deterministic processing order.

        Raises:
            RemoteSyncError: If the SSH command fails.
        """
        # Quote the directory path to handle spaces correctly.
        remote_dir_quoted = shlex.quote(self._remote_mp3_dir)
        result = self._ssh(f"ls -1 {remote_dir_quoted}")

        filenames: list[str] = []
        for line in result.stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.lower().endswith(".mp3"):
                filenames.append(line)

        filenames.sort()
        logger.info(
            "remote_syncer.listed_mp3s",
            count=len(filenames),
            remote_dir=self._remote_mp3_dir,
        )
        return filenames

    def pull_mp3(
        self, filename: str, local_dir: Path, *, from_claim_dir: bool = False,
    ) -> Path:
        """Download a single MP3 from the remote server to a local directory.

        Uses ``ssh cat`` rather than ``scp`` because:
        - It routes through the existing ControlMaster socket.
        - Filenames with spaces are handled cleanly via shell quoting.

        Args:
            filename: Basename of the MP3 file on the remote server.
            local_dir: Local directory to write the downloaded file into.
            from_claim_dir: If True, read from the ``.processing/`` claim
                subdirectory instead of the main MP3 directory.

        Returns:
            Path to the downloaded local file.

        Raises:
            RemoteSyncError: If the SSH command fails.
        """
        if from_claim_dir:
            remote_path = (
                self._remote_mp3_dir + "/" + _CLAIM_SUBDIR + "/" + filename
            )
        else:
            remote_path = self._remote_mp3_dir + "/" + filename
        remote_path_quoted = shlex.quote(remote_path)

        local_path = local_dir / filename

        logger.debug("remote_syncer.pulling", filename=filename, local_path=str(local_path))

        result = self._ssh(f"cat {remote_path_quoted}")

        local_path.write_bytes(result.stdout)

        logger.info(
            "remote_syncer.pulled",
            filename=filename,
            size_mb=round(len(result.stdout) / 1024 / 1024, 1),
        )
        return local_path

    def push_file(self, local_path: Path, remote_dir: str) -> str:
        """Upload a local file to a directory on the remote server.

        Uses SCP with the ControlMaster socket for connection reuse.

        Args:
            local_path: Absolute path to the local file to upload.
            remote_dir: Destination directory on the remote server.

        Returns:
            The full remote path where the file was placed
            (``remote_dir/filename``).

        Raises:
            RemoteSyncError: If the SCP command fails.
        """
        remote_dest = f"{self._host}:{shlex.quote(remote_dir)}/"

        cmd = [
            "scp",
            "-o", "ControlMaster=no",
            "-o", f"ControlPath={self._control_path}",
            "-o", "StrictHostKeyChecking=accept-new",
            str(local_path),
            remote_dest,
        ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RemoteSyncError(
                f"scp {local_path.name} → {remote_dir}",
                result.returncode,
                result.stderr.decode(errors="replace"),
            )

        remote_path = remote_dir + "/" + local_path.name
        logger.info(
            "remote_syncer.pushed",
            local_path=str(local_path),
            remote_path=remote_path,
        )
        return remote_path

    def insert_remote_track(self, track_data: dict) -> None:
        """Insert (or replace) a track row in the remote SQLite database.

        To avoid any shell-escaping issues with Russian lyrics, quotes,
        newlines, and other special characters, the track data is:

        1. Serialised to JSON (UTF-8).
        2. Base64-encoded to produce a plain ASCII blob.
        3. Piped as stdin to a small Python script running on the remote host.
        4. The script decodes, deserialises, and runs the SQL INSERT.

        Args:
            track_data: Dict matching the tracks table column names.

        Raises:
            RemoteSyncError: If the SSH command fails.
        """
        json_bytes = json.dumps(track_data, ensure_ascii=False).encode("utf-8")
        b64_str = base64.b64encode(json_bytes).decode("ascii")

        # The remote Python script is embedded here. The base64-encoded JSON is
        # inlined as a string literal in the script (not piped separately on
        # stdin), because ``python3 -`` reads ALL of stdin as the script — any
        # extra bytes would be interpreted as code and cause a SyntaxError.
        db_path_escaped = self._remote_db_path.replace("\\", "\\\\").replace('"', '\\"')
        remote_script = f"""
import base64
import json
import sqlite3

data = json.loads(base64.b64decode("{b64_str}").decode("utf-8"))

conn = sqlite3.connect("{db_path_escaped}")
try:
    conn.execute(
        \"\"\"
{_REMOTE_INSERT_SQL}
        \"\"\",
        data,
    )
    conn.commit()
finally:
    conn.close()
"""

        self._ssh("python3 -", input_bytes=remote_script.encode("utf-8"))

        logger.info(
            "remote_syncer.track_inserted",
            track_id=track_data.get("id"),
            artist=track_data.get("artist"),
            title=track_data.get("title"),
        )

    def delete_remote_file(self, remote_path: str) -> None:
        """Delete a file on the remote server.

        Args:
            remote_path: Full path to the file on the remote server.

        Raises:
            RemoteSyncError: If the SSH command fails.
        """
        remote_path_quoted = shlex.quote(remote_path)
        self._ssh(f"rm -f {remote_path_quoted}")
        logger.info("remote_syncer.deleted", remote_path=remote_path)

    def ensure_remote_setup(self) -> None:
        """Create required directories and the tracks table on the remote server.

        Creates:
        - ``remote_output_dir/``
        - ``remote_output_dir/instrumental/``
        - The ``tracks`` table in the remote SQLite database (if not present).

        Raises:
            RemoteSyncError: If any SSH command fails.
        """
        output_dir_quoted = shlex.quote(self._remote_output_dir)
        instrumental_dir_quoted = shlex.quote(self._remote_output_dir + "/instrumental")
        self._ssh(f"mkdir -p {output_dir_quoted} {instrumental_dir_quoted}")
        logger.info(
            "remote_syncer.dirs_created", remote_output_dir=self._remote_output_dir
        )

        # Run the schema creation via a remote Python script so we don't have
        # to deal with quoting multi-line SQL inside a shell command.
        db_path_escaped = self._remote_db_path.replace("\\", "\\\\").replace('"', '\\"')
        schema_sql_escaped = _REMOTE_DB_SCHEMA_SQL.replace("\\", "\\\\").replace('"', '\\"')
        remote_script = f"""
import sqlite3
conn = sqlite3.connect("{db_path_escaped}")
try:
    conn.executescript(\"\"\"{schema_sql_escaped}\"\"\")
finally:
    conn.close()
"""
        self._ssh("python3 -", input_bytes=remote_script.encode("utf-8"))
        logger.info(
            "remote_syncer.schema_ensured", remote_db_path=self._remote_db_path
        )

        # Create the claim directory for multi-worker coordination.
        self._ensure_claim_dir()

    def _ensure_claim_dir(self) -> None:
        """Create the ``.processing/`` claim directory inside remote_mp3_dir."""
        claim_dir = self._remote_mp3_dir + "/" + _CLAIM_SUBDIR
        self._ssh(f"mkdir -p {shlex.quote(claim_dir)}")
        logger.info("remote_syncer.claim_dir_created", claim_dir=claim_dir)

    def claim_mp3(self, filename: str) -> bool:
        """Atomically claim an MP3 by moving it into ``.processing/``.

        Uses Linux ``rename()`` semantics (atomic on the same filesystem).
        If two workers try to claim the same file simultaneously, exactly one
        ``mv`` succeeds and the other gets ``ENOENT`` because the source is
        gone.

        Args:
            filename: Basename of the MP3 file.

        Returns:
            True if the claim succeeded, False if the file was already
            claimed by another worker.
        """
        src = shlex.quote(self._remote_mp3_dir + "/" + filename)
        dst = shlex.quote(
            self._remote_mp3_dir + "/" + _CLAIM_SUBDIR + "/" + filename
        )
        result = self._ssh(f"mv -n {src} {dst}", check=False)
        claimed = result.returncode == 0
        if not claimed:
            logger.debug("remote_syncer.claim_lost", filename=filename)
        return claimed

    def unclaim_mp3(self, filename: str) -> None:
        """Move an MP3 from ``.processing/`` back to the main directory.

        Used for recovery: if processing fails, the file is returned so it
        can be retried on the next run (or by another worker).

        Args:
            filename: Basename of the MP3 file.
        """
        src = shlex.quote(
            self._remote_mp3_dir + "/" + _CLAIM_SUBDIR + "/" + filename
        )
        dst = shlex.quote(self._remote_mp3_dir + "/" + filename)
        self._ssh(f"mv {src} {dst}", check=False)
        logger.info("remote_syncer.unclaimed", filename=filename)

    def list_claimed_mp3s(self) -> list[str]:
        """List MP3 filenames currently in the ``.processing/`` claim directory.

        Returns:
            Sorted list of MP3 filenames that are currently claimed.
        """
        claim_dir = self._remote_mp3_dir + "/" + _CLAIM_SUBDIR
        result = self._ssh(f"ls -1 {shlex.quote(claim_dir)}", check=False)
        if result.returncode != 0:
            return []

        filenames: list[str] = []
        for line in result.stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.lower().endswith(".mp3"):
                filenames.append(line)
        filenames.sort()
        return filenames

    def check_remote_track_exists(self, track_id: str) -> bool:
        """Check whether a track is already present in the remote database.

        Args:
            track_id: The UUID string of the track to look up.

        Returns:
            True if the track exists in the remote DB, False otherwise.

        Raises:
            RemoteSyncError: If the SSH command fails.
        """
        db_path_escaped = self._remote_db_path.replace("\\", "\\\\").replace('"', '\\"')
        # track_id is a UUID string — only hex digits and hyphens, safe to embed.
        remote_script = f"""
import sqlite3
conn = sqlite3.connect("{db_path_escaped}")
try:
    cursor = conn.execute(
        "SELECT 1 FROM tracks WHERE id = ? LIMIT 1",
        ("{track_id}",),
    )
    print("1" if cursor.fetchone() else "0")
finally:
    conn.close()
"""
        result = self._ssh("python3 -", input_bytes=remote_script.encode("utf-8"))
        stdout = result.stdout.decode(errors="replace").strip()
        return stdout == "1"
