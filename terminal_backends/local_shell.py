import codecs
import os
import secrets
import select
import stat
import sys
import threading
import time
from pathlib import Path

from .base import BackendSettingSchema, BackendStartFieldSchema, TerminalBackendPlugin, TerminalBridge
from runtime_logging import log_message

try:
    from ptyprocess import PtyProcess
except Exception:
    PtyProcess = None

try:
    from winpty import PtyProcess as WinPtyProcess
except Exception:
    WinPtyProcess = None


def decode_local_shell_output(data, decoder=None, *, final=False):
    if isinstance(data, bytes):
        if decoder is not None:
            return decoder.decode(data, final=final)
        return data.decode('utf-8', errors='replace')
    if isinstance(data, str):
        return data
    return str(data)


class LocalFileTransferError(Exception):
    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code


LOCAL_FILE_REFERENCE_TTL_SECONDS = 5 * 60
LOCAL_FILE_REFERENCE_MAX_RECORDS = 4096
LOCAL_FILE_REFERENCE_TOKEN_BYTES = 12


class LocalShellBridge(TerminalBridge):
    connection_type = 'local_shell'

    def __init__(
        self,
        owner_session,
        terminal_id='main',
        shell_config=None,
        *,
        ssh_term,
        get_default_local_shell_config,
        runtime=None,
    ):
        super().__init__(owner_session, terminal_id, runtime=runtime)
        self.process = None
        self._ssh_term = ssh_term
        shell_config = shell_config or get_default_local_shell_config()[0]
        self.shell = shell_config['shell_display']
        self.shell_command = shell_config['shell_command']
        self.terminal_kind = shell_config['terminal_kind']
        self.terminal_label = shell_config['terminal_label']
        self._output_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._file_copy_lock = threading.Lock()
        self._file_refs_lock = threading.Lock()
        self._file_refs = {}

    def file_copy_endpoint(self):
        return {
            'route': 'local',
            'shell': self.shell,
            'platform': sys.platform,
        }

    def files_available(self):
        try:
            self._require_anchored_file_copy_support()
            return True
        except LocalFileTransferError:
            return False

    @staticmethod
    def _require_anchored_file_copy_support():
        required = (os.open, os.stat, os.unlink, os.link, os.rename)
        if (
            not hasattr(os, 'O_DIRECTORY')
            or not hasattr(os, 'O_NOFOLLOW')
            or any(function not in os.supports_dir_fd for function in required)
        ):
            raise LocalFileTransferError(
                'local_copy_platform_unsupported',
                'Files for Local Shell requires anchored POSIX file operations.',
            )

    @staticmethod
    def _directory_snapshot(path):
        attributes = path.stat()
        if not stat.S_ISDIR(attributes.st_mode):
            raise LocalFileTransferError(
                'local_copy_not_directory',
                'The local file directory is not available.',
            )
        return {
            'directory_device': attributes.st_dev,
            'directory_inode': attributes.st_ino,
        }

    @classmethod
    def _open_anchored_directory(cls, snapshot):
        cls._require_anchored_file_copy_support()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(snapshot['directory'], flags)
            attributes = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(attributes.st_mode)
                or attributes.st_dev != snapshot.get('directory_device')
                or attributes.st_ino != snapshot.get('directory_inode')
            ):
                raise LocalFileTransferError(
                    'local_copy_directory_changed',
                    'The approved local directory changed before the copy completed.',
                )
            return descriptor
        except LocalFileTransferError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise LocalFileTransferError(
                'local_copy_directory_changed',
                'The approved local directory could not be reopened safely.',
            ) from exc

    @staticmethod
    def _validate_local_file_path(path):
        if not isinstance(path, str) or not path:
            raise LocalFileTransferError('local_copy_invalid_path', 'Local file path is required.')
        if len(path.encode('utf-8', errors='ignore')) > 4096 \
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            raise LocalFileTransferError('local_copy_invalid_path', 'Local file path is invalid.')
        candidate = Path(path)
        if not candidate.is_absolute():
            raise LocalFileTransferError(
                'local_copy_absolute_path_required',
                'Local Shell file copies require an absolute path.',
            )
        if not candidate.name:
            raise LocalFileTransferError(
                'local_copy_invalid_path',
                'Local file path must name a file.',
            )
        return candidate

    @staticmethod
    def _validate_local_name(name):
        if not isinstance(name, str) or not name:
            raise LocalFileTransferError('local_files_invalid_name', 'Local file name is invalid.')
        if (
            name in {'.', '..'}
            or '/' in name
            or '\\' in name
            or len(name.encode('utf-8', errors='ignore')) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise LocalFileTransferError('local_files_invalid_name', 'Local file name is invalid.')
        return name

    def validate_files_name(self, name):
        return self._validate_local_name(name)

    @classmethod
    def _validate_local_directory_path(cls, path):
        if not isinstance(path, str) or not path:
            raise LocalFileTransferError('local_files_invalid_path', 'Local directory path is required.')
        if len(path.encode('utf-8', errors='ignore')) > 4096 \
                or any(ord(character) < 32 or ord(character) == 127 for character in path):
            raise LocalFileTransferError('local_files_invalid_path', 'Local directory path is invalid.')
        candidate = Path(path)
        if not candidate.is_absolute():
            raise LocalFileTransferError(
                'local_files_absolute_path_required',
                'Local Shell Files requires an absolute directory path.',
            )
        return candidate

    def _register_local_file_reference(self, file_snapshot):
        now = time.monotonic()
        with self._file_refs_lock:
            for existing_id, record in list(self._file_refs.items()):
                if record['expires_at'] <= now:
                    self._file_refs.pop(existing_id, None)
            while len(self._file_refs) >= LOCAL_FILE_REFERENCE_MAX_RECORDS:
                self._file_refs.pop(next(iter(self._file_refs)))
            file_id = 'localf_' + secrets.token_urlsafe(LOCAL_FILE_REFERENCE_TOKEN_BYTES)
            while file_id in self._file_refs:
                file_id = 'localf_' + secrets.token_urlsafe(LOCAL_FILE_REFERENCE_TOKEN_BYTES)
            self._file_refs[file_id] = {
                **file_snapshot,
                'expires_at': now + LOCAL_FILE_REFERENCE_TTL_SECONDS,
            }
        return file_id

    def resolve_local_file_reference(self, file_id):
        if not isinstance(file_id, str) or len(file_id) > 128:
            raise LocalFileTransferError(
                'local_files_reference_invalid',
                'The local file selection is invalid.',
            )
        now = time.monotonic()
        with self._file_refs_lock:
            record = self._file_refs.get(file_id)
            if not record or record['expires_at'] <= now:
                self._file_refs.pop(file_id, None)
                raise LocalFileTransferError(
                    'local_files_reference_expired',
                    'The local file selection expired. Refresh the directory and try again.',
                )
            return {
                key: value
                for key, value in record.items()
                if key != 'expires_at'
            }

    def browse_local_files(self, path=None, *, child=None, parent=False, max_entries=1000):
        self._require_anchored_file_copy_support()
        requested_path = Path.home() if path is None else self._validate_local_directory_path(path)
        if child is not None:
            requested_path = requested_path / self._validate_local_name(child)
        elif parent:
            requested_path = requested_path.parent
        try:
            canonical_path = requested_path.resolve(strict=True)
            attributes = canonical_path.stat()
            if not stat.S_ISDIR(attributes.st_mode):
                raise LocalFileTransferError(
                    'local_files_not_directory',
                    'Local path is not a directory.',
                )
            directories = []
            files = []
            truncated = False
            with os.scandir(canonical_path) as entries:
                for entry in entries:
                    try:
                        entry_name = self._validate_local_name(entry.name)
                        entry_attributes = entry.stat(follow_symlinks=False)
                    except (LocalFileTransferError, OSError):
                        continue
                    is_directory = stat.S_ISDIR(entry_attributes.st_mode)
                    is_regular_file = stat.S_ISREG(entry_attributes.st_mode)
                    if not is_directory and not is_regular_file:
                        continue
                    if len(directories) + len(files) >= max_entries:
                        truncated = True
                        break
                    if is_directory:
                        directories.append({'name': entry_name})
                        continue
                    file_path = canonical_path / entry_name
                    file_snapshot = self._local_file_snapshot(file_path, entry_attributes)
                    file_snapshot.update(self._directory_snapshot(canonical_path))
                    file_snapshot['endpoint'] = self.file_copy_endpoint()
                    files.append({
                        'file_id': self._register_local_file_reference(file_snapshot),
                        'name': entry_name,
                        'size': entry_attributes.st_size,
                        'mtime': entry_attributes.st_mtime,
                    })
            directories.sort(key=lambda item: item['name'].casefold())
            files.sort(key=lambda item: item['name'].casefold())
            return {
                'path': str(canonical_path),
                'directories': directories,
                'files': files,
                'truncated': truncated,
                'endpoint': self.file_copy_endpoint(),
            }
        except LocalFileTransferError:
            raise
        except FileNotFoundError as exc:
            raise LocalFileTransferError(
                'local_files_directory_not_found',
                'The local directory does not exist.',
            ) from exc
        except OSError as exc:
            raise LocalFileTransferError(
                'local_files_browse_failed',
                'The local directory could not be opened.',
            ) from exc

    def rename_local_file(self, file_snapshot, new_filename):
        new_filename = self._validate_local_name(new_filename)
        if new_filename == file_snapshot.get('filename'):
            raise LocalFileTransferError(
                'local_files_rename_unchanged',
                'Enter a different file name.',
            )
        with self._file_copy_lock:
            directory_descriptor = self._open_anchored_directory(file_snapshot)
            try:
                source_attributes = os.stat(
                    file_snapshot['filename'],
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                self._validate_local_snapshot(source_attributes, file_snapshot)
                try:
                    os.stat(new_filename, dir_fd=directory_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise LocalFileTransferError(
                        'local_files_rename_destination_exists',
                        'A file or directory with the new name already exists.',
                    )
                os.rename(
                    file_snapshot['filename'],
                    new_filename,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                return {
                    'status': 'completed',
                    'action': 'rename',
                    'source_path': file_snapshot['path'],
                    'destination_path': str(Path(file_snapshot['directory']) / new_filename),
                    'filename': new_filename,
                }
            except LocalFileTransferError:
                raise
            except FileNotFoundError as exc:
                raise LocalFileTransferError(
                    'local_files_file_not_found',
                    'The local file no longer exists.',
                ) from exc
            except OSError as exc:
                raise LocalFileTransferError(
                    'local_files_rename_failed',
                    'The local file could not be renamed.',
                ) from exc
            finally:
                os.close(directory_descriptor)

    def delete_local_file(self, file_snapshot):
        with self._file_copy_lock:
            directory_descriptor = self._open_anchored_directory(file_snapshot)
            try:
                source_attributes = os.stat(
                    file_snapshot['filename'],
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                self._validate_local_snapshot(source_attributes, file_snapshot)
                os.unlink(file_snapshot['filename'], dir_fd=directory_descriptor)
                return {
                    'status': 'completed',
                    'action': 'delete',
                    'deleted_path': file_snapshot['path'],
                    'filename': file_snapshot['filename'],
                }
            except LocalFileTransferError:
                raise
            except FileNotFoundError as exc:
                raise LocalFileTransferError(
                    'local_files_file_not_found',
                    'The local file no longer exists.',
                ) from exc
            except OSError as exc:
                raise LocalFileTransferError(
                    'local_files_delete_failed',
                    'The local file could not be deleted.',
                ) from exc
            finally:
                os.close(directory_descriptor)

    @staticmethod
    def _local_file_snapshot(path, attributes):
        return {
            'path': str(path),
            'directory': str(path.parent),
            'filename': path.name,
            'size': attributes.st_size,
            'mtime_ns': attributes.st_mtime_ns,
            'device': attributes.st_dev,
            'inode': attributes.st_ino,
        }

    @staticmethod
    def _validate_local_snapshot(attributes, snapshot, error_code='local_copy_file_changed'):
        if (
            not stat.S_ISREG(attributes.st_mode)
            or attributes.st_size != snapshot.get('size')
            or attributes.st_mtime_ns != snapshot.get('mtime_ns')
            or attributes.st_dev != snapshot.get('device')
            or attributes.st_ino != snapshot.get('inode')
        ):
            raise LocalFileTransferError(error_code, 'The local file changed after approval preflight.')

    @staticmethod
    def _keep_both_path(path, sequence):
        suffix = path.suffix
        stem = path.name[:-len(suffix)] if suffix else path.name
        return path.with_name(f'{stem} ({sequence}){suffix}')

    def prepare_local_file(self, path):
        self._require_anchored_file_copy_support()
        requested_path = self._validate_local_file_path(path)
        try:
            requested_attributes = requested_path.lstat()
            if stat.S_ISLNK(requested_attributes.st_mode):
                raise LocalFileTransferError(
                    'local_copy_file_symlink',
                    'Symbolic links are not supported for local file copies.',
                )
            canonical_path = requested_path.resolve(strict=True)
            attributes = canonical_path.stat()
            if not stat.S_ISREG(attributes.st_mode):
                raise LocalFileTransferError(
                    'local_copy_file_not_regular',
                    'The local source path is not a regular file.',
                )
            snapshot = self._local_file_snapshot(canonical_path, attributes)
            snapshot.update(self._directory_snapshot(canonical_path.parent))
            snapshot['endpoint'] = self.file_copy_endpoint()
            return snapshot
        except LocalFileTransferError:
            raise
        except FileNotFoundError as exc:
            raise LocalFileTransferError('local_copy_file_not_found', 'The local source file does not exist.') from exc
        except OSError as exc:
            raise LocalFileTransferError('local_copy_file_prepare_failed', 'The local source file could not be checked.') from exc

    def download_local_chunks(self, file_snapshot, chunk_size=65536):
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
        flags |= os.O_NOFOLLOW
        with self._file_copy_lock:
            directory_descriptor = self._open_anchored_directory(file_snapshot)
            try:
                descriptor = os.open(
                    file_snapshot['filename'],
                    flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                os.close(directory_descriptor)
                raise LocalFileTransferError('local_copy_file_open_failed', 'The local source file could not be opened.') from exc
            try:
                with os.fdopen(descriptor, 'rb', closefd=True) as source:
                    self._validate_local_snapshot(os.fstat(source.fileno()), file_snapshot)
                    remaining = file_snapshot['size']
                    while remaining > 0:
                        chunk = source.read(min(chunk_size, remaining))
                        if not chunk:
                            raise LocalFileTransferError(
                                'local_copy_read_incomplete',
                                'The local source file ended before the copy completed.',
                            )
                        remaining -= len(chunk)
                        yield chunk
                    self._validate_local_snapshot(os.fstat(source.fileno()), file_snapshot)
                final_attributes = os.stat(
                    file_snapshot['filename'],
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                self._validate_local_snapshot(final_attributes, file_snapshot)
            except LocalFileTransferError:
                raise
            except OSError as exc:
                raise LocalFileTransferError('local_copy_read_failed', 'The local source file could not be read.') from exc
            finally:
                os.close(directory_descriptor)

    def prepare_local_upload(self, path, conflict_mode='fail'):
        self._require_anchored_file_copy_support()
        requested_path = self._validate_local_file_path(path)
        if conflict_mode not in {'fail', 'keep_both', 'replace'}:
            raise LocalFileTransferError('local_copy_invalid_conflict_mode', 'Local copy conflict mode is invalid.')
        try:
            canonical_directory = requested_path.parent.resolve(strict=True)
            if not canonical_directory.is_dir():
                raise LocalFileTransferError(
                    'local_copy_not_directory',
                    'The local destination directory does not exist.',
                )
            selected_path = canonical_directory / requested_path.name
            existing_snapshot = None
            try:
                existing = selected_path.lstat()
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise LocalFileTransferError(
                        'local_copy_destination_symlink',
                        'A symbolic-link destination cannot be replaced.',
                    )
                if not stat.S_ISREG(existing.st_mode):
                    raise LocalFileTransferError(
                        'local_copy_destination_not_file',
                        'The local destination exists and is not a regular file.',
                    )
                existing_snapshot = self._local_file_snapshot(selected_path, existing)
                if conflict_mode == 'keep_both':
                    for sequence in range(1, 10000):
                        candidate = self._keep_both_path(selected_path, sequence)
                        if not candidate.exists() and not candidate.is_symlink():
                            selected_path = candidate
                            existing_snapshot = None
                            break
                    else:
                        raise LocalFileTransferError(
                            'local_copy_keep_both_exhausted',
                            'A unique local destination name could not be created.',
                        )
            return {
                'status': 'conflict' if existing_snapshot and conflict_mode == 'fail' else 'ready',
                'directory': str(canonical_directory),
                'filename': selected_path.name,
                'destination_path': str(selected_path),
                'replace': existing_snapshot is not None and conflict_mode == 'replace',
                'existing': existing_snapshot,
                'existing_size': existing_snapshot['size'] if existing_snapshot else None,
                'endpoint': self.file_copy_endpoint(),
                **self._directory_snapshot(canonical_directory),
            }
        except LocalFileTransferError:
            raise
        except OSError as exc:
            raise LocalFileTransferError(
                'local_copy_destination_prepare_failed',
                'The local destination could not be checked.',
            ) from exc

    def upload_local_stream(self, stream, upload, expected_size, before_read_callback=None,
                            progress_callback=None, pre_commit_callback=None):
        destination_path = upload['destination_path']
        destination_name = upload['filename']
        temporary_name = f'.standterm-copy-{secrets.token_hex(16)}'
        completed = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
        with self._file_copy_lock:
            directory_descriptor = self._open_anchored_directory(upload)
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                with os.fdopen(descriptor, 'wb', closefd=True) as destination:
                    transferred = 0
                    while transferred < expected_size:
                        if before_read_callback:
                            before_read_callback(transferred, expected_size)
                        chunk = stream.read(min(65536, expected_size - transferred))
                        if not chunk:
                            raise LocalFileTransferError(
                                'local_copy_read_incomplete',
                                'The source stream ended before the local copy completed.',
                            )
                        destination.write(chunk)
                        transferred += len(chunk)
                        if progress_callback:
                            progress_callback(transferred, expected_size)
                    destination.flush()
                    os.fsync(destination.fileno())
                temporary_attributes = os.stat(
                    temporary_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(temporary_attributes.st_mode) \
                        or temporary_attributes.st_size != expected_size:
                    raise LocalFileTransferError(
                        'local_copy_size_mismatch',
                        'The local temporary file size did not match the source.',
                    )

                try:
                    current = os.stat(
                        destination_name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    current = None
                expected_existing = upload.get('existing')
                if upload.get('replace'):
                    if current is None or not expected_existing:
                        raise LocalFileTransferError(
                            'local_copy_destination_changed',
                            'The local destination changed before commit.',
                        )
                    self._validate_local_snapshot(
                        current,
                        expected_existing,
                        error_code='local_copy_destination_changed',
                    )
                elif current is not None:
                    raise LocalFileTransferError(
                        'local_copy_destination_changed',
                        'The local destination was created before commit.',
                    )
                if pre_commit_callback:
                    pre_commit_callback(transferred, expected_size)
                if upload.get('replace'):
                    os.rename(
                        temporary_name,
                        destination_name,
                        src_dir_fd=directory_descriptor,
                        dst_dir_fd=directory_descriptor,
                    )
                else:
                    try:
                        os.link(
                            temporary_name,
                            destination_name,
                            src_dir_fd=directory_descriptor,
                            dst_dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise LocalFileTransferError(
                            'local_copy_atomic_create_failed',
                            'The local destination could not be created atomically.',
                        ) from exc
                completed = True
                if not upload.get('replace'):
                    try:
                        os.unlink(temporary_name, dir_fd=directory_descriptor)
                    except OSError:
                        pass
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
                return {
                    'destination_path': destination_path,
                    'filename': destination_name,
                    'bytes_written': expected_size,
                }
            except LocalFileTransferError:
                raise
            except OSError as exc:
                raise LocalFileTransferError('local_copy_write_failed', 'The local destination could not be written.') from exc
            finally:
                if not completed:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_descriptor)
                    except OSError:
                        pass
                os.close(directory_descriptor)

    def connect(self, cols=80, rows=24):
        if sys.platform.startswith('win'):
            return self._connect_windows(cols, rows)
        if PtyProcess is None:
            return False, {
                'message': 'Local Shell requires ptyprocess. Re-run the launcher with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = self._build_process_environment()
            cwd = str(Path.home())
            self.process = PtyProcess.spawn(
                self.shell_command,
                cwd=cwd,
                env=env,
                dimensions=(rows, cols),
            )
            log_message(f"[+] Local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            log_message(f"[!] Local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _build_process_environment(self):
        env = dict(os.environ)
        env['TERM'] = self._ssh_term
        env['COLORTERM'] = 'truecolor'
        env['TERM_PROGRAM'] = 'StandTerm'
        return env

    def _connect_windows(self, cols, rows):
        if WinPtyProcess is None:
            return False, {
                'message': 'Local Shell on Windows requires pywinpty. Re-run run.bat with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = self._build_process_environment()
            cwd = str(Path.home())
            self.process = self._spawn_windows_process(cols, rows, cwd, env)
            self.resize(cols, rows)
            log_message(f"[+] Windows local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            log_message(f"[!] Windows local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _spawn_windows_process(self, cols, rows, cwd, env):
        spawn_attempts = (
            lambda: WinPtyProcess.spawn(self.shell, cwd=cwd, env=env, dimensions=(rows, cols)),
            lambda: WinPtyProcess.spawn(self.shell, cwd=cwd, env=env),
            lambda: WinPtyProcess.spawn(self.shell, dimensions=(rows, cols)),
            lambda: WinPtyProcess.spawn(self.shell),
        )
        last_error = None
        for spawn in spawn_attempts:
            try:
                return spawn()
            except TypeError as exc:
                last_error = exc
        raise last_error

    def read_loop(self):
        log_message(f"[*] Starting local shell read loop for {self.sid}")
        while True:
            self.runtime.sleep(0.01)
            if not self.process:
                break

            if sys.platform.startswith('win'):
                if not self._read_windows_once():
                    break
                continue

            try:
                readable, _, _ = select.select([self.process.fd], [], [], 0)
                if not readable:
                    if self.closing:
                        break
                    if not self.process.isalive():
                        self.emit_output({
                            'message_type': 'ssh_closed',
                            'message': 'Local shell session closed.',
                        })
                        break
                    continue

                data = self.process.read(size=4096)
                if data:
                    decoded = decode_local_shell_output(data, self._output_decoder)
                    if not decoded:
                        continue
                    self.emit_output({
                        'message_type': 'terminal',
                        'data': decoded,
                    })
            except EOFError:
                if self.closing:
                    break
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell session closed.',
                })
                break
            except Exception as exc:
                if self.closing:
                    break
                log_message(f"[!] Local shell read error: {exc}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell closed due to a read error.',
                    'error_code': 'local_shell_read_error',
                })
                break

        log_message(f"[*] Local shell read loop terminated for {self.sid}")
        self.runtime.unregister_bridge(self.owner_session, self.terminal_id, self)

    def _read_windows_once(self):
        try:
            data = self.process.read(4096)
            if data:
                self.emit_output({
                    'message_type': 'terminal',
                    'data': data,
                })
            if not self.process.isalive():
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell session closed.',
                })
                return False
            return True
        except EOFError:
            if self.closing:
                return False
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell session closed.',
            })
            return False
        except Exception as exc:
            if self.closing:
                return False
            log_message(f"[!] Windows local shell read error: {exc}")
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell closed due to a read error.',
                'error_code': 'local_shell_read_error',
            })
            return False

    def write(self, data):
        if self.process:
            try:
                if not sys.platform.startswith('win') and isinstance(data, str):
                    data = data.encode('utf-8')
                self.process.write(data)
            except Exception as exc:
                log_message(f"[!] Local shell write error: {exc}")

    def resize(self, cols, rows):
        if self.process:
            try:
                if sys.platform.startswith('win'):
                    if hasattr(self.process, 'set_size'):
                        self.process.set_size(cols, rows)
                    elif hasattr(self.process, 'setwinsize'):
                        self.process.setwinsize(rows, cols)
                    elif hasattr(self.process, 'resize'):
                        self.process.resize(cols, rows)
                else:
                    self.process.setwinsize(rows, cols)
            except Exception as exc:
                log_message(f"[!] Local shell resize error: {exc}")

    def close(self):
        if not self.process:
            return
        try:
            if sys.platform.startswith('win') and hasattr(self.process, 'terminate'):
                self.process.terminate()
            elif sys.platform.startswith('win') and hasattr(self.process, 'kill'):
                self.process.kill()
            else:
                self.process.close(force=True)
        except TypeError:
            self.process.close()
        except Exception:
            pass
        self.process = None


class LocalShellBackendPlugin(TerminalBackendPlugin):
    connection_type = 'local_shell'
    label = 'Local Shell'

    def __init__(
        self,
        *,
        bridge_cls,
        is_allowed_for_client,
        get_local_shell_config,
        bridge_kwargs,
        is_wsl,
        get_wsl_local_shell_options,
        default_shell_kind,
        low_risk_settings_capability,
        high_risk_settings_capability,
    ):
        self._bridge_cls = bridge_cls
        self._is_allowed_for_client = is_allowed_for_client
        self._get_local_shell_config = get_local_shell_config
        self._bridge_kwargs = bridge_kwargs
        self._is_wsl = is_wsl
        self._get_wsl_local_shell_options = get_wsl_local_shell_options
        self._default_shell_kind = default_shell_kind
        self._low_risk_settings_capability = low_risk_settings_capability
        self._high_risk_settings_capability = high_risk_settings_capability

    def get_settings_schema(self):
        allowed_kinds = [item['kind'] for item in self._get_wsl_local_shell_options()] if self._is_wsl() else []
        return [
            BackendSettingSchema(
                setting_key='local_shell.default_kind',
                label='Default shell kind',
                value_type='enum',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_shell_kind,
                allowed_values=tuple(allowed_kinds),
                restart_required=False,
                readonly_when_remote=True,
                mutable=bool(allowed_kinds),
            ),
            BackendSettingSchema(
                setting_key='local_shell.remote_access',
                label='Remote Local Shell access',
                value_type='boolean',
                risk_level='high',
                required_capability=self._high_risk_settings_capability,
                default_value=False,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
        ]

    def _get_default_shell_kind(self, context=None):
        settings_snapshot = context.settings_snapshot if context else None
        if isinstance(settings_snapshot, dict):
            value = settings_snapshot.get('local_shell.default_kind')
            if value is not None:
                normalized, error = self.validate_setting_update(
                    'local_shell.default_kind',
                    value,
                    current_value=self._default_shell_kind,
                )
                if not error:
                    return normalized
        return self._default_shell_kind

    def build_policy_option(self, context=None, browser_authorized=False):
        client_ip = context.client_ip if context else 'unknown'
        browser_authorized = context.browser_authorized if context else browser_authorized
        default_shell_kind = self._get_default_shell_kind(context=context)
        allowed = self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        option = {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
        }
        if self._is_wsl():
            option['shell_options'] = self._get_wsl_local_shell_options()
            option['default_shell_kind'] = default_shell_kind
        return option

    def get_start_form_schema(self, context=None):
        if not self._is_wsl():
            return []
        shell_options = self._get_wsl_local_shell_options()
        return [
            BackendStartFieldSchema(
                name='local_shell_kind',
                label='Shell',
                value_type='enum',
                input_type='select',
                default_value=self._get_default_shell_kind(context=context),
                required=False,
                options=tuple(
                    {
                        'value': item['kind'],
                        'label': item.get('label') or item['kind'],
                    }
                    for item in shell_options
                ),
            ),
        ]

    def validate_setting_update(self, setting_key, value, current_value=None):
        if setting_key != 'local_shell.default_kind':
            return super().validate_setting_update(setting_key, value, current_value=current_value)
        if not self._is_wsl():
            return None, {
                'error_code': 'settings_not_mutable',
                'message': 'Local Shell default kind is only mutable on WSL.',
            }
        allowed_kinds = [item['kind'] for item in self._get_wsl_local_shell_options()]
        normalized = value.strip().lower() if isinstance(value, str) else ''
        if normalized not in allowed_kinds:
            return None, {
                'error_code': 'settings_invalid_value',
                'message': 'Local Shell default kind must be bash, cmd, or powershell.',
            }
        return normalized, None

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False, context=None):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'Local Shell is not available for this client.',
                'error_code': 'local_shell_unavailable_for_client',
            }
        requested_kind = data.get('local_shell_kind')
        if not isinstance(requested_kind, str) or not requested_kind.strip():
            requested_kind = self._get_default_shell_kind(context=context)
            if not self._is_wsl():
                requested_kind = None
        shell_config, shell_error = self._get_local_shell_config(requested_kind)
        if shell_error:
            return None, shell_error
        return {'local_shell_config': shell_config}, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(
            session_token,
            terminal_id,
            shell_config=payload.get('local_shell_config'),
            **self._bridge_kwargs,
        )

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)
