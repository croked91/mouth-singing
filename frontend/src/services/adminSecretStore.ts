const ADMIN_SECRET_STORAGE_KEY = 'alignmentAdminSecret';

function readStorage(storage: Storage | undefined): string {
  try {
    return storage?.getItem(ADMIN_SECRET_STORAGE_KEY)?.trim() ?? '';
  } catch {
    return '';
  }
}

function writeStorage(storage: Storage | undefined, value: string): void {
  try {
    if (value) storage?.setItem(ADMIN_SECRET_STORAGE_KEY, value);
    else storage?.removeItem(ADMIN_SECRET_STORAGE_KEY);
  } catch {
    // Storage may be unavailable in private contexts. The in-memory state still works.
  }
}

export function getStoredAdminSecret(): string {
  return readStorage(window.sessionStorage) || readStorage(window.localStorage);
}

export function setStoredAdminSecret(value: string): void {
  const normalized = value.trim();
  writeStorage(window.sessionStorage, normalized);
  writeStorage(window.localStorage, normalized);
}

export function clearStoredAdminSecret(): void {
  writeStorage(window.sessionStorage, '');
  writeStorage(window.localStorage, '');
}
