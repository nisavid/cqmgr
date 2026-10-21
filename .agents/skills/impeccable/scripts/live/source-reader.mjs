import fs from 'node:fs';
import path from 'node:path';

export class SourceReadError extends Error {
  /** Carry a source-reader failure's HTTP status and public response text. */
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** Check strict descendant containment using native path-segment boundaries. */
function isBelow(root, target) {
  const relative = path.relative(root, target);
  return relative !== '' && relative !== '..'
    && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

/**
 * Read regular project files, including safe in-project symlink aliases.
 * Canonical containment blocks links committed by an untrusted checkout.
 * Descriptor identity checks also reject replacements observed during open;
 * this is not a sandbox for a concurrent process with filesystem write access.
 */
export function readProjectSource(cwd, filePath) {
  if (typeof filePath !== 'string' || !filePath || filePath.includes('\0')
      || path.isAbsolute(filePath) || path.win32.isAbsolute(filePath)) {
    throw new SourceReadError(400, 'Bad path');
  }
  const root = fs.realpathSync(cwd);
  const requested = path.resolve(root, filePath);
  if (!isBelow(root, requested)) throw new SourceReadError(403, 'Forbidden');

  let fd;
  try {
    const target = fs.realpathSync(requested);
    if (!isBelow(root, target)) throw new SourceReadError(403, 'Forbidden');
    const expected = fs.lstatSync(target, { bigint: true });
    if (!expected.isFile()) throw new SourceReadError(403, 'Forbidden');
    fd = fs.openSync(target, fs.constants.O_RDONLY
      | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
    const opened = fs.fstatSync(fd, { bigint: true });
    if (!opened.isFile() || opened.dev !== expected.dev || opened.ino !== expected.ino
        || !isBelow(root, fs.realpathSync(target))) {
      throw new SourceReadError(403, 'Forbidden');
    }
    // Read the checked object, never reopen its pathname.
    return fs.readFileSync(fd, 'utf-8');
  } catch (error) {
    if (error instanceof SourceReadError) throw error;
    if (['ENOENT', 'ENOTDIR'].includes(error.code)) {
      throw new SourceReadError(404, 'File not found');
    }
    throw new SourceReadError(403, 'Forbidden');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}
