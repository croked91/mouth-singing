from __future__ import annotations

import asyncio
import pathlib
import tempfile

import structlog

from karaoke_shared.models.track import SyllableTiming

logger = structlog.get_logger(__name__)

# ASS resolution constants.
_PLAY_RES_X = 1920
_PLAY_RES_Y = 1080

# Maximum line length in characters before wrapping to a new subtitle line.
_MAX_LINE_CHARS = 45

# Minimum pause between words (seconds) that triggers a new subtitle line.
_LINE_BREAK_PAUSE_SEC = 0.5

# How many centiseconds of padding to add after the last syllable on a line.
_LINE_END_PADDING_CS = 50  # 0.5 seconds


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert a float seconds value to ASS timestamp format H:MM:SS.cc."""
    total_cs = int(round(seconds * 100))
    centiseconds = total_cs % 100
    total_seconds = total_cs // 100
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


class VideoGenerator:
    """Generates karaoke MP4 clips with syllable-highlighted ASS subtitles.

    Uses FFmpeg with a dark background color source and the ASS subtitle
    filter to render a full karaoke video from an instrumental audio track.

    Args:
        media_root: Root directory where media files are stored.  Output
            clips are written to {media_root}/clips/{track_id}.mp4.
    """

    def __init__(self, media_root: str) -> None:
        self.media_root = media_root

    async def generate(
        self,
        instrumental_path: str,
        syllable_timings: list[SyllableTiming],
        artist: str,
        title: str,
        track_id: str,
    ) -> str:
        """Generate a karaoke MP4 clip and return its path.

        Creates an ASS subtitle file from the syllable timings, then runs
        FFmpeg to combine the instrumental audio with the subtitle overlay
        on a dark background.

        Args:
            instrumental_path: Path to the instrumental audio file.
            syllable_timings: List of syllable timings in seconds.
            artist: Track artist name (shown in the title card).
            title: Track title (shown in the title card).
            track_id: Unique track ID used to name the output file.

        Returns:
            Absolute path to the generated MP4 clip.
        """
        clips_dir = pathlib.Path(self.media_root) / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        output_path = clips_dir / f"{track_id}.mp4"

        # Write ASS to a temporary file that lives for the duration of ffmpeg.
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tmp:
            ass_path = tmp.name

        try:
            self._generate_ass(syllable_timings, artist, title, ass_path)
            await self._run_ffmpeg(instrumental_path, ass_path, str(output_path))
        finally:
            pathlib.Path(ass_path).unlink(missing_ok=True)

        logger.info(
            "video_generated",
            track_id=track_id,
            output_path=str(output_path),
        )
        return str(output_path)

    # ------------------------------------------------------------------
    # ASS generation
    # ------------------------------------------------------------------

    def _generate_ass(
        self,
        syllable_timings: list[SyllableTiming],
        artist: str,
        title: str,
        ass_path: str,
    ) -> None:
        """Write an ASS subtitle file with karaoke \\k tags.

        Syllables are grouped into lines of at most _MAX_LINE_CHARS characters
        or split at natural pauses longer than _LINE_BREAK_PAUSE_SEC seconds.
        Each Dialogue event contains one line rendered with \\k tags that
        specify per-syllable durations in centiseconds.

        Args:
            syllable_timings: Syllable timings in seconds.
            artist: Artist name for the title card.
            title: Track title for the title card.
            ass_path: Destination path for the ASS file.
        """
        header = self._ass_header()
        title_card = self._ass_title_card(artist, title)
        dialogue_lines = self._build_dialogue_lines(syllable_timings)

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(title_card)
            for line in dialogue_lines:
                f.write(line)

    def _ass_header(self) -> str:
        return (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {_PLAY_RES_X}\n"
            f"PlayResY: {_PLAY_RES_Y}\n"
            "WrapStyle: 0\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            # Karaoke style: white primary, #F0ABFC (ASS BGR: &H00FCABF0) secondary
            "Style: Karaoke,Arial,72,&H00FFFFFF,&H00FCABF0,&H00080505,"
            "&H80080505,-1,0,0,0,100,100,0,0,1,3,0,2,60,60,80,1\n"
            # Title card style
            "Style: Title,Arial,48,&H00FFFFFF,&H00FFFFFF,&H00080505,"
            "&H80080505,-1,0,0,0,100,100,0,0,1,2,0,2,60,60,80,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    def _ass_title_card(self, artist: str, title: str) -> str:
        return (
            f"Dialogue: 0,0:00:00.00,0:00:03.00,Title,,0,0,0,,{artist} \u2014 {title}\n"
        )

    def _build_dialogue_lines(
        self, syllable_timings: list[SyllableTiming]
    ) -> list[str]:
        """Group syllable timings into ASS Dialogue lines with \\k tags."""
        if not syllable_timings:
            return []

        lines: list[str] = []
        groups = self._group_into_lines(syllable_timings)

        for group in groups:
            if not group:
                continue

            line_start = group[0].start
            line_end = group[-1].end + _LINE_END_PADDING_CS / 100.0

            text_parts: list[str] = []
            for timing in group:
                duration_cs = int(round((timing.end - timing.start) * 100))
                duration_cs = max(duration_cs, 1)
                text_parts.append(f"{{\\k{duration_cs}}}{timing.syllable}")

            text = "".join(text_parts)
            start_str = _seconds_to_ass_time(line_start)
            end_str = _seconds_to_ass_time(line_end)
            lines.append(
                f"Dialogue: 0,{start_str},{end_str},Karaoke,,0,0,0,,{text}\n"
            )

        return lines

    def _group_into_lines(
        self, syllable_timings: list[SyllableTiming]
    ) -> list[list[SyllableTiming]]:
        """Split syllable timings into groups representing subtitle lines.

        A new line is started when:
        - The accumulated character count would exceed _MAX_LINE_CHARS, or
        - The gap between the end of the previous syllable and the start of
          the next exceeds _LINE_BREAK_PAUSE_SEC seconds.
        """
        groups: list[list[SyllableTiming]] = []
        current_group: list[SyllableTiming] = []
        current_chars = 0

        for i, timing in enumerate(syllable_timings):
            syllable_len = len(timing.syllable)

            # Check for a natural pause before this syllable.
            if current_group and i > 0:
                gap = timing.start - syllable_timings[i - 1].end
                if gap > _LINE_BREAK_PAUSE_SEC:
                    groups.append(current_group)
                    current_group = []
                    current_chars = 0

            # Check if adding this syllable would overflow the line.
            if current_group and current_chars + syllable_len > _MAX_LINE_CHARS:
                groups.append(current_group)
                current_group = []
                current_chars = 0

            current_group.append(timing)
            current_chars += syllable_len

        if current_group:
            groups.append(current_group)

        return groups

    # ------------------------------------------------------------------
    # FFmpeg execution
    # ------------------------------------------------------------------

    async def _run_ffmpeg(
        self,
        instrumental_path: str,
        ass_path: str,
        output_path: str,
    ) -> None:
        """Run FFmpeg to combine audio and ASS subtitles into an MP4.

        Uses a dark near-black background (0x050508) as the video source.
        The ASS subtitle filter overlays the karaoke text.

        Args:
            instrumental_path: Path to the instrumental audio.
            ass_path: Path to the generated ASS subtitle file.
            output_path: Destination path for the MP4 output.

        Raises:
            RuntimeError: If FFmpeg exits with a non-zero return code.
        """
        # Escape the ASS path for use in the -vf filter expression.
        # FFmpeg's filter parser treats \, :, [, ], ; and , as special characters.
        safe_ass_path = (
            ass_path
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace(",", "\\,")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace(";", "\\;")
        )

        cmd = [
            "ffmpeg",
            "-f", "lavfi",
            "-i", "color=c=0x050508:s=1920x1080:r=30",
            "-i", instrumental_path,
            "-vf", f"ass={safe_ass_path}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-y",
            output_path,
        ]

        logger.info("ffmpeg_start", output_path=output_path)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=600.0
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("FFmpeg timed out after 600 seconds")

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            logger.error(
                "ffmpeg_failed",
                returncode=process.returncode,
                stderr=stderr_text[-2000:],
            )
            raise RuntimeError(
                f"FFmpeg exited with code {process.returncode}: {stderr_text[-500:]}"
            )

        logger.info("ffmpeg_finished", output_path=output_path)
