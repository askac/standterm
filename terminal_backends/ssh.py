import base64
import codecs
import getpass
import hashlib
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from .base import BackendAction, BackendSettingSchema, BackendStartFieldSchema, TerminalBackendPlugin, TerminalBridge
from runtime_logging import log_message


SSH_PROFILE_NAME_MAX_LENGTH = 64
SSH_BROWSER_KEY_ID_MAX_LENGTH = 128
SSH_BROWSER_KEY_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')
SFTP_FILE_REFERENCE_TTL_SECONDS = 5 * 60
SFTP_FILE_REFERENCE_MAX_RECORDS = 4096
SFTP_FILE_REFERENCE_TOKEN_BYTES = 12
SFTP_IO_TIMEOUT_SECONDS = 60


class BrowserSSHKeyError(Exception):
    pass


class SFTPTransferError(Exception):
    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code


class BrowserEd25519Key:
    name = 'ssh-ed25519'
    public_blob = None

    def __init__(self, paramiko_module, public_key, sign_callback):
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise BrowserSSHKeyError('Browser Ed25519 public key must be 32 bytes.')
        self._paramiko = paramiko_module
        self._public_key = public_key
        self._sign_callback = sign_callback
        self._verifier = paramiko_module.Ed25519Key(data=self.asbytes())

    def asbytes(self):
        message = self._paramiko.Message()
        message.add_string(self.name)
        message.add_string(self._public_key)
        return message.asbytes()

    def get_name(self):
        return self.name

    def get_bits(self):
        return 256

    def get_fingerprint(self):
        return hashlib.md5(self.asbytes()).digest()

    def can_sign(self):
        return True

    def sign_ssh_data(self, data, algorithm=None):
        if algorithm != self.name:
            raise BrowserSSHKeyError('Browser SSH key only supports ssh-ed25519 signatures.')
        try:
            signature = self._sign_callback(data, algorithm)
        except BrowserSSHKeyError:
            raise
        except Exception as exc:
            raise BrowserSSHKeyError(str(exc)) from exc
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise BrowserSSHKeyError('Browser Ed25519 signature must be 64 bytes.')
        signature_message = self._paramiko.Message()
        signature_message.add_string(self.name)
        signature_message.add_string(signature)
        verifier_message = self._paramiko.Message(signature_message.asbytes())
        if not self._verifier.verify_ssh_sig(data, verifier_message):
            raise BrowserSSHKeyError('Browser SSH signature verification failed.')
        return signature_message


class SSHBridge(TerminalBridge):
    connection_type = 'ssh'
    terminal_kind = 'ssh'
    terminal_label = 'SSH'

    def __init__(
        self,
        owner_session,
        terminal_id='main',
        *,
        get_paramiko,
        ssh_term,
        local_public_key_types,
        request_browser_signature=None,
    ):
        super().__init__(owner_session, terminal_id)
        self._get_paramiko = get_paramiko
        self._ssh_term = ssh_term
        self._local_public_key_types = local_public_key_types
        self._request_browser_signature = request_browser_signature
        self._browser_signer_sid = None
        self._sftp_lock = threading.Lock()
        self._sftp_file_refs_lock = threading.Lock()
        self._sftp_file_refs = {}
        self._sftp_endpoint = None
        self.ssh = None
        self.auth_method = None
        self._reset_ssh_client()
        self.channel = None
        self._output_decoder = codecs.getincrementaldecoder('utf-8')(errors='ignore')

    def metadata(self, cols=None, rows=None):
        metadata = super().metadata(cols=cols, rows=rows)
        if self.auth_method:
            metadata['auth_method'] = self.auth_method
        return metadata

    def sftp_endpoint(self):
        return dict(self._sftp_endpoint) if self._sftp_endpoint else None

    def files_available(self):
        return True

    @staticmethod
    def _validate_sftp_path(path):
        if not isinstance(path, str):
            raise SFTPTransferError('sftp_invalid_path', 'Remote path is invalid.')
        if not path:
            raise SFTPTransferError('sftp_invalid_path', 'Remote path is required.')
        if len(path.encode('utf-8', errors='ignore')) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            raise SFTPTransferError('sftp_invalid_path', 'Remote path is invalid.')
        return path

    @staticmethod
    def _validate_sftp_name(name):
        SSHBridge._validate_sftp_path(name)
        if name in {'.', '..'} or '/' in name or '\\' in name:
            raise SFTPTransferError('sftp_invalid_filename', 'File name is invalid.')
        if len(name.encode('utf-8', errors='ignore')) > 255:
            raise SFTPTransferError('sftp_invalid_filename', 'File name is too long.')
        return name

    @staticmethod
    def _join_sftp_path(directory, name):
        if directory.endswith('/'):
            return directory + name
        return directory + '/' + name

    @staticmethod
    def _is_sftp_not_found(exc):
        return isinstance(exc, FileNotFoundError) or getattr(exc, 'errno', None) == 2

    @staticmethod
    def _keep_both_name(filename, sequence):
        dot_index = filename.rfind('.')
        if dot_index > 0:
            stem = filename[:dot_index]
            suffix = filename[dot_index:]
        else:
            stem = filename
            suffix = ''
        return f'{stem} ({sequence}){suffix}'

    def _open_sftp(self):
        transport = self.ssh.get_transport() if self.ssh else None
        if not transport or not transport.is_active():
            raise SFTPTransferError('sftp_connection_closed', 'The SSH connection is closed.')
        try:
            sftp = self.ssh.open_sftp()
            sftp.get_channel().settimeout(SFTP_IO_TIMEOUT_SECONDS)
            return sftp
        except Exception as exc:
            raise SFTPTransferError('sftp_unavailable', 'SFTP is unavailable on this SSH server.') from exc

    @staticmethod
    def _close_sftp_in_background(sftp):
        def close_sftp():
            try:
                sftp.close()
            except Exception as exc:
                log_message(f'[!] SFTP session cleanup failed: {exc}')

        threading.Thread(
            target=close_sftp,
            daemon=True,
            name='standterm-sftp-close',
        ).start()

    def _canonical_sftp_directory(self, sftp, directory):
        canonical_directory = self._validate_sftp_path(sftp.normalize(directory))
        directory_stat = sftp.stat(canonical_directory)
        if directory_stat.st_mode is None or not stat.S_ISDIR(directory_stat.st_mode):
            raise SFTPTransferError('sftp_not_directory', 'Remote path is not a directory.')
        return canonical_directory

    def _get_sftp_regular_file(self, sftp, directory, filename):
        path = self._join_sftp_path(directory, filename)
        try:
            attributes = sftp.lstat(path)
        except Exception as exc:
            if self._is_sftp_not_found(exc):
                raise SFTPTransferError('sftp_file_not_found', 'The remote file no longer exists.') from exc
            raise
        if attributes.st_mode is not None and stat.S_ISLNK(attributes.st_mode):
            raise SFTPTransferError('sftp_file_symlink', 'Symbolic links are not supported for this operation.')
        if attributes.st_mode is None or not stat.S_ISREG(attributes.st_mode):
            raise SFTPTransferError('sftp_file_not_regular', 'The remote path is not a regular file.')
        return path, attributes

    @staticmethod
    def _validate_sftp_file_snapshot(attributes, expected_size, expected_mtime):
        if attributes.st_size != expected_size or attributes.st_mtime != expected_mtime:
            raise SFTPTransferError('sftp_file_changed', 'The remote file changed after the directory was listed.')

    def _register_sftp_file_reference(self, file_snapshot):
        now = time.monotonic()
        with self._sftp_file_refs_lock:
            for existing_id, record in list(self._sftp_file_refs.items()):
                if record['expires_at'] <= now:
                    self._sftp_file_refs.pop(existing_id, None)
            while len(self._sftp_file_refs) >= SFTP_FILE_REFERENCE_MAX_RECORDS:
                self._sftp_file_refs.pop(next(iter(self._sftp_file_refs)))
            file_id = 'sftpf_' + secrets.token_urlsafe(SFTP_FILE_REFERENCE_TOKEN_BYTES)
            while file_id in self._sftp_file_refs:
                file_id = 'sftpf_' + secrets.token_urlsafe(SFTP_FILE_REFERENCE_TOKEN_BYTES)
            self._sftp_file_refs[file_id] = {
                **file_snapshot,
                'expires_at': now + SFTP_FILE_REFERENCE_TTL_SECONDS,
            }
        return file_id

    def resolve_sftp_file_reference(self, file_id):
        if not isinstance(file_id, str) or len(file_id) > 128:
            raise SFTPTransferError('sftp_file_reference_invalid', 'The remote file selection is invalid.')
        now = time.monotonic()
        with self._sftp_file_refs_lock:
            record = self._sftp_file_refs.get(file_id)
            if not record or record['expires_at'] <= now:
                self._sftp_file_refs.pop(file_id, None)
                raise SFTPTransferError('sftp_file_reference_expired', 'The remote file selection expired. Refresh the directory and try again.')
            return {
                key: value
                for key, value in record.items()
                if key != 'expires_at'
            }

    def browse_sftp(self, path=None, *, child=None, parent=False, max_entries=1000):
        requested_path = '.' if path is None else self._validate_sftp_path(path)
        if child is not None:
            child = self._validate_sftp_name(child)
            requested_path = self._join_sftp_path(requested_path, child)
        elif parent:
            requested_path = self._join_sftp_path(requested_path, '..')

        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                canonical_path = self._canonical_sftp_directory(sftp, requested_path)
                directories = []
                files = []
                truncated = False
                for entry in sftp.listdir_iter(canonical_path, read_aheads=10):
                    try:
                        entry_name = self._validate_sftp_name(entry.filename)
                    except SFTPTransferError:
                        continue
                    is_directory = entry.st_mode is not None and stat.S_ISDIR(entry.st_mode)
                    is_regular_file = entry.st_mode is not None and stat.S_ISREG(entry.st_mode)
                    if not is_directory and not is_regular_file:
                        continue
                    if len(directories) + len(files) >= max_entries:
                        truncated = True
                        break
                    if is_directory:
                        directories.append({
                            'name': entry_name,
                            'mtime': entry.st_mtime,
                        })
                    elif entry.st_size is not None and entry.st_mtime is not None:
                        file_snapshot = {
                            'directory': canonical_path,
                            'filename': entry_name,
                            'path': self._join_sftp_path(canonical_path, entry_name),
                            'size': entry.st_size,
                            'mtime': entry.st_mtime,
                            'endpoint': self.sftp_endpoint(),
                        }
                        files.append({
                            'file_id': self._register_sftp_file_reference(file_snapshot),
                            'name': entry_name,
                            'size': entry.st_size,
                            'mtime': entry.st_mtime,
                        })
                directories.sort(key=lambda item: item['name'].casefold())
                files.sort(key=lambda item: item['name'].casefold())
                return {
                    'path': canonical_path,
                    'directories': directories,
                    'files': files,
                    'truncated': truncated,
                    'endpoint': self.sftp_endpoint(),
                }
            except SFTPTransferError:
                raise
            except Exception as exc:
                raise SFTPTransferError('sftp_browse_failed', f'Remote directory could not be opened: {exc}') from exc
            finally:
                sftp.close()

    def prepare_sftp_file(self, directory, filename):
        directory = self._validate_sftp_path(directory)
        filename = self._validate_sftp_name(filename)
        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                canonical_directory = self._canonical_sftp_directory(sftp, directory)
                path, attributes = self._get_sftp_regular_file(sftp, canonical_directory, filename)
                return {
                    'directory': canonical_directory,
                    'filename': filename,
                    'path': path,
                    'size': attributes.st_size,
                    'mtime': attributes.st_mtime,
                    'endpoint': self.sftp_endpoint(),
                }
            except SFTPTransferError:
                raise
            except Exception as exc:
                raise SFTPTransferError('sftp_file_prepare_failed', f'Remote file could not be checked: {exc}') from exc
            finally:
                sftp.close()

    def download_sftp_chunks(self, file_snapshot, chunk_size=65536):
        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                path, attributes = self._get_sftp_regular_file(
                    sftp,
                    file_snapshot['directory'],
                    file_snapshot['filename'],
                )
                self._validate_sftp_file_snapshot(
                    attributes,
                    file_snapshot['size'],
                    file_snapshot['mtime'],
                )
                remaining = file_snapshot['size']
                with sftp.open(path, 'rb') as remote_file:
                    while remaining > 0:
                        chunk = remote_file.read(min(chunk_size, remaining))
                        if not chunk:
                            raise SFTPTransferError('sftp_download_incomplete', 'The remote file ended before the download completed.')
                        remaining -= len(chunk)
                        yield chunk
                final_attributes = sftp.lstat(path)
                self._validate_sftp_file_snapshot(
                    final_attributes,
                    file_snapshot['size'],
                    file_snapshot['mtime'],
                )
            finally:
                sftp.close()

    def rename_sftp_file(self, directory, filename, new_filename, expected_size, expected_mtime):
        directory = self._validate_sftp_path(directory)
        filename = self._validate_sftp_name(filename)
        new_filename = self._validate_sftp_name(new_filename)
        if new_filename == filename:
            raise SFTPTransferError('sftp_rename_unchanged', 'Enter a different file name.')
        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                canonical_directory = self._canonical_sftp_directory(sftp, directory)
                source_path, attributes = self._get_sftp_regular_file(sftp, canonical_directory, filename)
                self._validate_sftp_file_snapshot(attributes, expected_size, expected_mtime)
                destination_path = self._join_sftp_path(canonical_directory, new_filename)
                try:
                    sftp.lstat(destination_path)
                except Exception as exc:
                    if not self._is_sftp_not_found(exc):
                        raise
                else:
                    raise SFTPTransferError('sftp_rename_destination_exists', 'A file with the new name already exists.')
                sftp.rename(source_path, destination_path)
                return {
                    'status': 'completed',
                    'action': 'rename',
                    'source_path': source_path,
                    'destination_path': destination_path,
                    'filename': new_filename,
                }
            except SFTPTransferError:
                raise
            except Exception as exc:
                raise SFTPTransferError('sftp_rename_failed', f'Remote file could not be renamed: {exc}') from exc
            finally:
                sftp.close()

    def delete_sftp_file(self, directory, filename, expected_size, expected_mtime):
        directory = self._validate_sftp_path(directory)
        filename = self._validate_sftp_name(filename)
        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                canonical_directory = self._canonical_sftp_directory(sftp, directory)
                path, attributes = self._get_sftp_regular_file(sftp, canonical_directory, filename)
                self._validate_sftp_file_snapshot(attributes, expected_size, expected_mtime)
                sftp.remove(path)
                return {
                    'status': 'completed',
                    'action': 'delete',
                    'deleted_path': path,
                    'filename': filename,
                }
            except SFTPTransferError:
                raise
            except Exception as exc:
                raise SFTPTransferError('sftp_delete_failed', f'Remote file could not be deleted: {exc}') from exc
            finally:
                sftp.close()

    def prepare_sftp_upload(self, directory, filename, conflict_mode='ask'):
        directory = self._validate_sftp_path(directory)
        filename = self._validate_sftp_name(filename)
        if conflict_mode not in {'ask', 'keep_both', 'replace'}:
            raise SFTPTransferError('sftp_invalid_conflict_mode', 'Upload conflict mode is invalid.')

        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                canonical_directory = self._validate_sftp_path(sftp.normalize(directory))
                directory_stat = sftp.stat(canonical_directory)
                if directory_stat.st_mode is None or not stat.S_ISDIR(directory_stat.st_mode):
                    raise SFTPTransferError('sftp_not_directory', 'Remote path is not a directory.')
                selected_name = filename
                destination_path = self._join_sftp_path(canonical_directory, selected_name)
                existing = None
                try:
                    existing = sftp.lstat(destination_path)
                except Exception as exc:
                    if not self._is_sftp_not_found(exc):
                        raise

                if existing is not None:
                    if existing.st_mode is not None and stat.S_ISLNK(existing.st_mode):
                        raise SFTPTransferError('sftp_destination_symlink', 'The destination is a symbolic link and cannot be replaced.')
                    if existing.st_mode is None or not stat.S_ISREG(existing.st_mode):
                        raise SFTPTransferError('sftp_destination_not_file', 'The destination exists and is not a regular file.')
                    if conflict_mode == 'ask':
                        return {
                            'status': 'conflict',
                            'directory': canonical_directory,
                            'filename': selected_name,
                            'destination_path': destination_path,
                            'existing_size': existing.st_size,
                            'existing_mtime': existing.st_mtime,
                            'endpoint': self.sftp_endpoint(),
                        }
                    if conflict_mode == 'keep_both':
                        for sequence in range(1, 10000):
                            candidate = self._keep_both_name(filename, sequence)
                            candidate_path = self._join_sftp_path(canonical_directory, candidate)
                            try:
                                sftp.lstat(candidate_path)
                            except Exception as exc:
                                if self._is_sftp_not_found(exc):
                                    selected_name = candidate
                                    destination_path = candidate_path
                                    existing = None
                                    break
                                raise
                        else:
                            raise SFTPTransferError('sftp_keep_both_exhausted', 'A unique destination name could not be created.')

                return {
                    'status': 'ready',
                    'directory': canonical_directory,
                    'filename': selected_name,
                    'destination_path': destination_path,
                    'replace': existing is not None and conflict_mode == 'replace',
                    'existing_size': existing.st_size if existing is not None else None,
                    'existing_mtime': existing.st_mtime if existing is not None else None,
                    'endpoint': self.sftp_endpoint(),
                }
            except SFTPTransferError:
                raise
            except Exception as exc:
                raise SFTPTransferError('sftp_upload_prepare_failed', f'Upload destination could not be checked: {exc}') from exc
            finally:
                sftp.close()

    def upload_sftp_stream(self, stream, upload, expected_size, before_read_callback=None,
                           progress_callback=None, pre_commit_callback=None,
                           report_publish_outcome_unknown=False):
        destination_path = upload['destination_path']
        filename = upload['filename']
        replace = bool(upload.get('replace'))
        expected_existing_size = upload.get('existing_size')
        expected_existing_mtime = upload.get('existing_mtime')
        temporary_path = self._join_sftp_path(
            upload['directory'],
            f'.standterm-upload-{os.urandom(16).hex()}',
        )
        completed = False

        with self._sftp_lock:
            sftp = self._open_sftp()
            try:
                try:
                    current = sftp.lstat(destination_path)
                except Exception as exc:
                    if self._is_sftp_not_found(exc):
                        current = None
                    else:
                        raise
                if replace:
                    if (
                        current is None
                        or current.st_mode is None
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_size != expected_existing_size
                        or current.st_mtime != expected_existing_mtime
                    ):
                        raise SFTPTransferError('sftp_destination_changed', 'The destination changed before upload started.')
                elif current is not None:
                    raise SFTPTransferError('sftp_destination_changed', 'The destination was created before upload started.')

                transferred = 0
                with sftp.open(temporary_path, 'wx') as remote_file:
                    while transferred < expected_size:
                        if before_read_callback:
                            before_read_callback(transferred, expected_size)
                        chunk = stream.read(min(65536, expected_size - transferred))
                        if not chunk:
                            raise SFTPTransferError('sftp_upload_incomplete', 'The upload ended before the complete file was received.')
                        remote_file.write(chunk)
                        transferred += len(chunk)
                        if progress_callback:
                            progress_callback(transferred, expected_size)
                    remote_file.flush()

                uploaded_stat = sftp.stat(temporary_path)
                if uploaded_stat.st_size != expected_size:
                    raise SFTPTransferError('sftp_upload_size_mismatch', 'The uploaded file size did not match the source file.')
                try:
                    current = sftp.lstat(destination_path)
                except Exception as exc:
                    if self._is_sftp_not_found(exc):
                        current = None
                    else:
                        raise
                if replace:
                    if (
                        current is None
                        or current.st_mode is None
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_size != expected_existing_size
                        or current.st_mtime != expected_existing_mtime
                    ):
                        raise SFTPTransferError('sftp_destination_changed', 'The destination changed before upload commit.')
                elif current is not None:
                    raise SFTPTransferError('sftp_destination_changed', 'The destination was created before upload commit.')
                if pre_commit_callback:
                    pre_commit_callback(transferred, expected_size)

                def publish_is_definitely_unapplied():
                    try:
                        temporary = sftp.lstat(temporary_path)
                        if (
                            temporary.st_mode is None
                            or not stat.S_ISREG(temporary.st_mode)
                            or temporary.st_size != expected_size
                        ):
                            return False
                        try:
                            destination = sftp.lstat(destination_path)
                        except Exception as exc:
                            if self._is_sftp_not_found(exc):
                                destination = None
                            else:
                                return False
                        if not replace:
                            return destination is None
                        return (
                            destination is not None
                            and destination.st_mode is not None
                            and stat.S_ISREG(destination.st_mode)
                            and destination.st_size == expected_existing_size
                            and destination.st_mtime == expected_existing_mtime
                        )
                    except Exception:
                        return False

                try:
                    if replace:
                        sftp.posix_rename(temporary_path, destination_path)
                    else:
                        sftp.rename(temporary_path, destination_path)
                except Exception as exc:
                    if report_publish_outcome_unknown and not publish_is_definitely_unapplied():
                        raise SFTPTransferError(
                            'file_copy_publish_outcome_unknown',
                            'The SFTP server did not confirm whether the destination was updated.',
                        ) from exc
                    if replace:
                        raise SFTPTransferError(
                            'sftp_atomic_replace_unavailable',
                            'This SFTP server cannot replace the existing file atomically.',
                        ) from exc
                    raise
                completed = True
                return {
                    'destination_path': destination_path,
                    'filename': filename,
                    'bytes_written': expected_size,
                }
            except SFTPTransferError:
                raise
            except Exception as exc:
                raise SFTPTransferError('sftp_upload_failed', f'File upload failed: {exc}') from exc
            finally:
                if not completed:
                    try:
                        sftp.remove(temporary_path)
                    except Exception:
                        pass
                self._close_sftp_in_background(sftp)

    def set_browser_signer_sid(self, sid):
        if self._browser_signer_sid is not None and self._browser_signer_sid != sid:
            raise BrowserSSHKeyError('Browser SSH signer is already assigned.')
        self._browser_signer_sid = sid

    def _reset_ssh_client(self, trust_unknown_host=False):
        paramiko_module = self._get_paramiko()
        if self.ssh:
            self.ssh.close()
        self.ssh = paramiko_module.SSHClient()
        self.ssh.load_system_host_keys()
        if trust_unknown_host:
            self.ssh.set_missing_host_key_policy(paramiko_module.AutoAddPolicy())
        else:
            self.ssh.set_missing_host_key_policy(paramiko_module.RejectPolicy())

    @staticmethod
    def _is_local_target(host):
        if not host:
            return False
        normalized = host.strip().lower()
        return normalized in {'127.0.0.1', 'localhost', '::1'}

    @staticmethod
    def _iter_local_private_key_files():
        ssh_dir = Path.home() / '.ssh'
        key_names = (
            'id_ed25519',
            'id_ecdsa',
            'id_rsa',
            'id_dsa',
            'id_ed25519_sk',
            'id_ecdsa_sk',
        )
        for key_name in key_names:
            key_path = ssh_dir / key_name
            if key_path.is_file():
                yield key_path

    @staticmethod
    def _iter_local_public_key_files():
        ssh_dir = Path.home() / '.ssh'
        key_names = (
            'id_ed25519.pub',
            'id_ecdsa.pub',
            'id_rsa.pub',
            'id_dsa.pub',
            'id_ed25519_sk.pub',
            'id_ecdsa_sk.pub',
        )
        for key_name in key_names:
            key_path = ssh_dir / key_name
            if key_path.is_file():
                yield key_path

    def _parse_public_key_line(self, line):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            return None

        parts = stripped.split()
        for index in range(len(parts) - 1):
            key_type = parts[index]
            key_body = parts[index + 1]
            if key_type not in self._local_public_key_types:
                continue
            try:
                base64.b64decode(key_body.encode('ascii'), validate=True)
            except Exception:
                continue
            return {
                'key_type': key_type,
                'key_body': key_body,
                'line': stripped,
            }
        return None

    def _get_local_public_key_entries(self):
        entries = []
        for key_path in self._iter_local_public_key_files():
            try:
                line = key_path.read_text(encoding='utf-8').strip()
            except OSError:
                continue
            parsed = self._parse_public_key_line(line)
            if parsed:
                parsed['path'] = key_path
                entries.append(parsed)
        return entries

    def _get_authorized_keys_path(self):
        return Path.home() / '.ssh' / 'authorized_keys'

    def _read_authorized_key_fingerprints(self):
        authorized_keys_path = self._get_authorized_keys_path()
        fingerprints = set()
        if not authorized_keys_path.is_file():
            return fingerprints

        try:
            lines = authorized_keys_path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return fingerprints

        for line in lines:
            parsed = self._parse_public_key_line(line)
            if parsed:
                fingerprints.add((parsed['key_type'], parsed['key_body']))
        return fingerprints

    def _get_missing_local_public_keys(self):
        authorized_fingerprints = self._read_authorized_key_fingerprints()
        missing_entries = []
        for entry in self._get_local_public_key_entries():
            fingerprint = (entry['key_type'], entry['key_body'])
            if fingerprint not in authorized_fingerprints:
                missing_entries.append(entry)
        return missing_entries

    def _can_offer_local_key_setup(self, user):
        availability = self._get_local_key_setup_availability(user)
        return availability['can_offer']

    def _get_local_key_setup_availability(self, user):
        current_user = getpass.getuser()
        if user != current_user:
            return {
                'can_offer': False,
                'reason': 'Automatic localhost key setup is only available for the current local user.',
                'error_code': 'localhost_key_setup_unsupported_user',
            }

        if os.name == 'nt':
            return {
                'can_offer': False,
                'reason': (
                    'Automatic localhost key setup is not supported on native Windows yet. '
                    'Windows OpenSSH may require a different authorized keys file, such as '
                    '%USERPROFILE%\\.ssh\\authorized_keys for a regular user or '
                    'C:\\ProgramData\\ssh\\administrators_authorized_keys for an administrator '
                    'account. Please add your public key manually, then try again.'
                ),
                'error_code': 'localhost_key_setup_unsupported_windows',
            }

        return {'can_offer': True}

    def _append_public_key_entry_to_authorized_keys(self, entry):
        fingerprint = (entry['key_type'], entry['key_body'])
        if fingerprint in self._read_authorized_key_fingerprints():
            return False, {
                'status': 'already_configured',
                'message': 'Your local public key is already present in ~/.ssh/authorized_keys.',
            }

        ssh_dir = Path.home() / '.ssh'
        authorized_keys_path = self._get_authorized_keys_path()

        try:
            ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(ssh_dir, 0o700)

            existing_text = ''
            if authorized_keys_path.exists():
                existing_text = authorized_keys_path.read_text(encoding='utf-8')

            with authorized_keys_path.open('a', encoding='utf-8') as authorized_keys_file:
                if existing_text and not existing_text.endswith('\n'):
                    authorized_keys_file.write('\n')
                authorized_keys_file.write(entry['line'] + '\n')
            os.chmod(authorized_keys_path, 0o600)
        except OSError as exc:
            return False, {
                'status': 'failed',
                'message': f'Failed to update ~/.ssh/authorized_keys: {exc}',
            }

        return True, {
            'status': 'success',
            'message': (
                f'Added {entry["path"].name} to ~/.ssh/authorized_keys. '
                'Try connecting to localhost again.'
            ),
        }

    def _append_local_public_key_to_authorized_keys(self):
        missing_entries = self._get_missing_local_public_keys()
        if not missing_entries:
            return False, {
                'status': 'already_configured',
                'message': 'Your local public key is already present in ~/.ssh/authorized_keys.',
            }

        return self._append_public_key_entry_to_authorized_keys(missing_entries[0])

    def prepare_backend_action(self, action_type, payload, expires_at, message=None, question=None):
        if action_type != 'offer_localhost_key_setup':
            return None
        missing_entries = self._get_missing_local_public_keys()
        if not missing_entries:
            return None
        return BackendAction(
            action_type=action_type,
            terminal_id=payload['terminal_id'],
            metadata={
                'host': payload['host'],
                'port': payload['port'],
                'username': payload['username'],
                'key_entry': missing_entries[0],
            },
            expires_at=expires_at,
            message=message,
            question=question,
        )

    @classmethod
    def execute_backend_action(cls, action, **bridge_kwargs):
        if action.action_type != 'offer_localhost_key_setup':
            return {
                'status': 'failed',
                'message': 'Unsupported SSH backend action.',
                'error_code': 'backend_action_unsupported',
            }
        metadata = action.metadata if isinstance(action.metadata, dict) else {}
        username = metadata.get('username')
        key_entry = metadata.get('key_entry')
        bridge = cls(None, action.terminal_id, **bridge_kwargs)
        if not isinstance(key_entry, dict):
            return {
                'status': 'failed',
                'message': 'Localhost key setup action is invalid.',
                'error_code': 'localhost_key_setup_invalid_action',
            }
        if not bridge._can_offer_local_key_setup(username):
            return {
                'status': 'failed',
                'message': 'Automatic localhost key setup is only available for the current local user.',
                'error_code': 'localhost_key_setup_unavailable',
            }
        _, result = bridge._append_public_key_entry_to_authorized_keys(key_entry)
        return result

    def _build_local_key_setup_hint(self):
        message = (
            'Local public key authentication for localhost failed, and your local public key was not '
            'found in ~/.ssh/authorized_keys on this machine. Add your public key to '
            '~/.ssh/authorized_keys, or enter your SSH password and try again.'
        )
        question = (
            'Do you want to add your public key to ~/.ssh/authorized_keys?'
        )
        return {
            'message': message,
            'error_code': 'localhost_key_not_authorized',
            'action_type': 'offer_localhost_key_setup',
            'action_message': message,
            'action_question': question,
        }

    @staticmethod
    def _build_manual_local_key_setup_hint(reason, error_code):
        return {
            'message': reason,
            'error_code': error_code,
        }

    def _load_private_key(self, key_path, passphrase=None):
        paramiko_module = self._get_paramiko()
        key_types = []
        for key_type_name in ('Ed25519Key', 'ECDSAKey', 'RSAKey', 'DSSKey'):
            key_type = getattr(paramiko_module, key_type_name, None)
            if key_type is not None:
                key_types.append(key_type)
        last_error = None
        for key_type in key_types:
            try:
                return key_type.from_private_key_file(str(key_path), password=passphrase)
            except paramiko_module.PasswordRequiredException:
                raise
            except paramiko_module.SSHException as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise paramiko_module.SSHException(f"Unsupported key format: {key_path}")

    def _connect_with_local_keys(self, host, port, user, password):
        paramiko_module = self._get_paramiko()
        auth_errors = []
        passphrase = password or None

        try:
            self._reset_ssh_client(trust_unknown_host=True)
            self.ssh.connect(
                host,
                port=int(port),
                username=user,
                password=None,
                timeout=15,
                allow_agent=True,
                look_for_keys=True,
            )
            log_message(f"[+] Local key auth succeeded via agent/default keys for {self.sid}")
            return True, None
        except paramiko_module.AuthenticationException as exc:
            auth_errors.append(f"agent/default keys: {exc}")
        except Exception as exc:
            auth_errors.append(f"agent/default keys: {exc}")

        for key_path in self._iter_local_private_key_files():
            try:
                pkey = self._load_private_key(key_path, passphrase=passphrase)
            except paramiko_module.PasswordRequiredException:
                auth_errors.append(f"{key_path.name}: passphrase required")
                continue
            except Exception as exc:
                auth_errors.append(f"{key_path.name}: {exc}")
                continue

            try:
                self._reset_ssh_client(trust_unknown_host=True)
                self.ssh.connect(
                    host,
                    port=int(port),
                    username=user,
                    password=None,
                    pkey=pkey,
                    timeout=15,
                    allow_agent=False,
                    look_for_keys=False,
                )
                log_message(f"[+] Local key auth succeeded via {key_path.name} for {self.sid}")
                return True, None
            except Exception as exc:
                auth_errors.append(f"{key_path.name}: {exc}")

        return False, '; '.join(auth_errors)

    def _connect_with_browser_key(self, host, port, user, browser_key, is_localhost):
        if not self._browser_signer_sid or not self._request_browser_signature:
            raise BrowserSSHKeyError('Browser SSH signer is unavailable.')
        paramiko_module = self._get_paramiko()
        public_key = base64.b64decode(browser_key['public_key'].encode('ascii'), validate=True)
        signer_key = BrowserEd25519Key(
            paramiko_module,
            public_key,
            lambda data, algorithm: self._request_browser_signature(
                self,
                self._browser_signer_sid,
                browser_key,
                data,
                algorithm,
            ),
        )
        self._reset_ssh_client(trust_unknown_host=is_localhost)
        self.ssh.connect(
            host,
            port=int(port),
            username=user,
            password=None,
            pkey=signer_key,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        self.auth_method = 'browser-key'

    def connect(self, host, port, user, password=None, browser_key=None, cols=80, rows=24):
        paramiko_module = self._get_paramiko()
        try:
            pwd = password if password else ""
            log_message(f"[*] Attempting SSH connection for {user!r} at {host!r}:{port}...")

            is_localhost = self._is_local_target(host)
            if browser_key:
                self._connect_with_browser_key(host, port, user, browser_key, is_localhost)
            elif is_localhost and not pwd:
                success, key_error = self._connect_with_local_keys(host, port, user, None)
                if not success:
                    setup_availability = self._get_local_key_setup_availability(user)
                    if setup_availability['can_offer']:
                        missing_local_keys = self._get_missing_local_public_keys()
                        if missing_local_keys:
                            hint = self._build_local_key_setup_hint()
                            log_message(f"[*] Local key auth failed for {self.sid}; offering localhost key setup.")
                            return False, hint
                    elif setup_availability.get('reason'):
                        log_message(f"[*] Local key auth failed for {self.sid}; auto setup unavailable.")
                        return False, self._build_manual_local_key_setup_hint(
                            setup_availability['reason'],
                            setup_availability.get('error_code'),
                        )
                    raise paramiko_module.AuthenticationException(
                        f"Local public key auth failed: {key_error or 'no usable local key found'}"
                    )
                self.auth_method = 'host-key'
            else:
                self._reset_ssh_client(trust_unknown_host=is_localhost)
                self.ssh.connect(
                    host,
                    port=int(port),
                    username=user,
                    password=pwd,
                    timeout=15,
                    allow_agent=False,
                    look_for_keys=False,
                )
                self.auth_method = 'password'

            self.channel = self.ssh.invoke_shell(term=self._ssh_term, width=cols, height=rows)
            self.channel.setblocking(0)
            self._sftp_endpoint = {
                'user': str(user),
                'host': str(host),
                'port': int(port),
                'route': 'direct',
            }
            log_message(f"[+] SSH connection established for {self.sid}")
            return True, None
        except BrowserSSHKeyError as exc:
            log_message(f"[!] Browser SSH key error: {exc}")
            return False, {
                'message': str(exc),
                'error_code': 'ssh_browser_key_failed',
            }
        except Exception as e:
            error_msg = str(e)
            log_message(f"[!] SSH Connection Error: {error_msg}")
            return False, {'message': error_msg}

    def read_loop(self):
        log_message(f"[*] Starting SSH read loop for {self.sid}")
        while True:
            # Short sleep to prevent CPU hogging while allowing high responsiveness
            self.runtime.sleep(0.01)
            if not self.channel:
                break

            try:
                if self.channel.recv_ready():
                    data = self._output_decoder.decode(self.channel.recv(4096))
                    if data:
                        self.emit_output({
                            'message_type': 'terminal',
                            'data': data,
                        })

                if self.channel.exit_status_ready():
                    log_message(f"[*] SSH session exited for {self.sid}")
                    self.emit_output({
                        'message_type': 'ssh_closed',
                        'message': 'SSH session closed.',
                    })
                    break
            except Exception as e:
                if self.closing:
                    break
                log_message(f"[!] Read error: {e}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'SSH connection closed due to a read error.',
                    'error_code': 'ssh_read_error',
                })
                break
        log_message(f"[*] SSH read loop terminated for {self.sid}")
        self.runtime.unregister_bridge(self.owner_session, self.terminal_id, self)

    def write(self, data):
        if self.channel:
            try:
                self.channel.send(data)
            except Exception as e:
                log_message(f"[!] Write error: {e}")

    def resize(self, cols, rows):
        if self.channel:
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception as e:
                log_message(f"[!] Resize error: {e}")

    def close(self):
        with self._sftp_file_refs_lock:
            self._sftp_file_refs.clear()
        self._sftp_endpoint = None
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None
        if self.ssh:
            try:
                self.ssh.close()
            except Exception:
                pass


class SSHBackendPlugin(TerminalBackendPlugin):
    connection_type = 'ssh'
    label = 'SSH'

    def __init__(
        self,
        *,
        bridge_cls,
        default_host,
        default_port,
        default_user,
        max_host_length,
        max_username_length,
        max_password_bytes,
        has_control_chars,
        is_allowed_for_client,
        is_browser_key_allowed,
        allowed_action_types,
        backend_action_store,
        bridge_kwargs,
        low_risk_settings_capability,
        high_risk_settings_capability,
        key_setup_ttl_seconds,
        token_urlsafe,
        time_func,
    ):
        self._bridge_cls = bridge_cls
        self._default_host = default_host
        self._default_port = default_port
        self._default_user = default_user
        self._max_host_length = max_host_length
        self._max_username_length = max_username_length
        self._max_password_bytes = max_password_bytes
        self._has_control_chars = has_control_chars
        self._is_allowed_for_client = is_allowed_for_client
        self._is_browser_key_allowed = is_browser_key_allowed
        self._allowed_action_types = allowed_action_types
        self._backend_action_store = backend_action_store
        self._bridge_kwargs = bridge_kwargs
        self._low_risk_settings_capability = low_risk_settings_capability
        self._high_risk_settings_capability = high_risk_settings_capability
        self._key_setup_ttl_seconds = key_setup_ttl_seconds
        self._token_urlsafe = token_urlsafe
        self._time_func = time_func

    def build_policy_option(self, context=None, browser_authorized=False):
        client_ip = context.client_ip if context else 'unknown'
        browser_authorized = context.browser_authorized if context else browser_authorized
        allowed = self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        return {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
            'browser_key_allowed': bool(
                allowed and self._is_browser_key_allowed(
                    client_ip,
                    browser_authorized=browser_authorized,
                )
            ),
        }

    def get_settings_schema(self):
        return [
            BackendSettingSchema(
                setting_key='ssh.default_host',
                label='Default host',
                value_type='string',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_host,
                restart_required=False,
                apply_scope='next_connection',
                readonly_when_remote=True,
                mutable=True,
            ),
            BackendSettingSchema(
                setting_key='ssh.default_port',
                label='Default port',
                value_type='integer',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_port,
                min_value=1,
                max_value=65535,
                restart_required=False,
                apply_scope='next_connection',
                readonly_when_remote=True,
                mutable=True,
            ),
            BackendSettingSchema(
                setting_key='ssh.default_user',
                label='Default user',
                value_type='string',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_user,
                restart_required=False,
                apply_scope='next_connection',
                readonly_when_remote=True,
                mutable=True,
            ),
            BackendSettingSchema(
                setting_key='ssh.localhost_key_setup_action',
                label='Localhost key setup action',
                value_type='boolean',
                risk_level='medium',
                required_capability=self._high_risk_settings_capability,
                default_value='offer_localhost_key_setup' in self._allowed_action_types,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
        ]

    def _get_default_host(self, context=None):
        settings_snapshot = context.settings_snapshot if context else None
        if isinstance(settings_snapshot, dict):
            value = settings_snapshot.get('ssh.default_host')
            if value is not None:
                normalized, error = self.validate_setting_update(
                    'ssh.default_host',
                    value,
                    current_value=self._default_host,
                )
                if not error:
                    return normalized
        return self._default_host

    def _get_default_port(self, context=None):
        settings_snapshot = context.settings_snapshot if context else None
        if isinstance(settings_snapshot, dict):
            value = settings_snapshot.get('ssh.default_port')
            if value is not None:
                normalized, error = self.validate_setting_update(
                    'ssh.default_port',
                    value,
                    current_value=self._default_port,
                )
                if not error:
                    return normalized
        return self._default_port

    def _get_default_user(self, context=None):
        settings_snapshot = context.settings_snapshot if context else None
        if isinstance(settings_snapshot, dict):
            value = settings_snapshot.get('ssh.default_user')
            if value is not None:
                normalized, error = self.validate_setting_update(
                    'ssh.default_user',
                    value,
                    current_value=self._default_user,
                )
                if not error:
                    return normalized
        return self._default_user

    def get_start_form_schema(self, context=None):
        default_host = self._get_default_host(context=context)
        default_port = self._get_default_port(context=context)
        default_user = self._get_default_user(context=context)
        return [
            BackendStartFieldSchema(
                name='host',
                label='Host',
                value_type='string',
                input_type='text',
                default_value=default_host,
                required=True,
                max_length=self._max_host_length,
            ),
            BackendStartFieldSchema(
                name='port',
                label='Port',
                value_type='integer',
                input_type='text',
                default_value=default_port,
                required=True,
                min_value=1,
                max_value=65535,
            ),
            BackendStartFieldSchema(
                name='username',
                label='Username',
                value_type='string',
                input_type='text',
                default_value=default_user,
                required=True,
                max_length=self._max_username_length,
            ),
            BackendStartFieldSchema(
                name='password',
                label='Password',
                value_type='string',
                input_type='password',
                required=False,
                secret=True,
                max_bytes=self._max_password_bytes,
            ),
        ]

    def validate_setting_update(self, setting_key, value, current_value=None):
        if setting_key == 'ssh.default_host':
            if not isinstance(value, str):
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default host must be a string.',
                }
            host = value.strip()
            if not host or len(host) > self._max_host_length:
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default host is empty or too long.',
                }
            if self._has_control_chars(host):
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default host contains invalid control characters.',
                }
            return host, None

        if setting_key == 'ssh.default_port':
            try:
                port = int(value)
            except (TypeError, ValueError):
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default port must be a number.',
                }
            if port < 1 or port > 65535:
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default port must be between 1 and 65535.',
                }
            return port, None

        if setting_key == 'ssh.default_user':
            if not isinstance(value, str):
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default user must be a string.',
                }
            user = value.strip()
            if not user or len(user) > self._max_username_length:
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default user is empty or too long.',
                }
            if self._has_control_chars(user):
                return None, {
                    'error_code': 'settings_invalid_value',
                    'message': 'SSH default user contains invalid control characters.',
                }
            return user, None

        return super().validate_setting_update(setting_key, value, current_value=current_value)

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False, context=None):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'SSH access requires a local client or browser authorization.',
                'error_code': 'ssh_remote_unauthorized',
            }

        host = data.get('host', self._get_default_host(context=context))
        if not isinstance(host, str):
            return None, 'Host must be a string.'
        host = host.strip()
        if not host or len(host) > self._max_host_length:
            return None, 'Host is empty or too long.'
        if self._has_control_chars(host):
            return None, 'Host contains invalid control characters.'

        try:
            port = int(data.get('port', self._get_default_port(context=context)))
        except (TypeError, ValueError):
            return None, 'Port must be a number.'
        if port < 1 or port > 65535:
            return None, 'Port must be between 1 and 65535.'

        user = data.get('username', self._get_default_user(context=context))
        if not isinstance(user, str):
            return None, 'Username must be a string.'
        user = user.strip()
        if not user or len(user) > self._max_username_length:
            return None, 'Username is empty or too long.'
        if self._has_control_chars(user):
            return None, 'Username contains invalid control characters.'

        password = data.get('password') or ''
        if not isinstance(password, str):
            return None, 'Password must be a string.'
        if len(password.encode('utf-8', errors='ignore')) > self._max_password_bytes:
            return None, 'Password is too long.'

        profile_name = data.get('profile_name')
        if profile_name is not None:
            if not isinstance(profile_name, str):
                return None, 'SSH profile name must be a string.'
            profile_name = profile_name.strip()
            if len(profile_name) > SSH_PROFILE_NAME_MAX_LENGTH:
                return None, f'SSH profile name must be {SSH_PROFILE_NAME_MAX_LENGTH} characters or fewer.'
            if self._has_control_chars(profile_name):
                return None, 'SSH profile name contains invalid control characters.'

        use_browser_key = data.get('use_browser_key', False)
        if not isinstance(use_browser_key, bool):
            return None, 'Use browser key must be a boolean.'
        browser_key = None
        if use_browser_key:
            if not self._is_browser_key_allowed(client_ip, browser_authorized=browser_authorized):
                return None, {
                    'message': 'Browser SSH keys require a local browser or an authorized HTTPS connection.',
                    'error_code': 'ssh_browser_key_insecure_transport',
                }
            if password:
                return None, 'Password must be empty when browser key authentication is selected.'
            profile_id = data.get('profile_id')
            key_id = data.get('key_id')
            for field_name, field_value in (('SSH profile id', profile_id), ('SSH key id', key_id)):
                if (
                    not isinstance(field_value, str)
                    or not field_value
                    or len(field_value) > SSH_BROWSER_KEY_ID_MAX_LENGTH
                    or not SSH_BROWSER_KEY_ID_PATTERN.fullmatch(field_value)
                ):
                    return None, f'{field_name} is invalid.'
            public_key = data.get('browser_public_key')
            if not isinstance(public_key, str):
                return None, 'Browser SSH public key must be a Base64 string.'
            try:
                public_key_bytes = base64.b64decode(public_key.encode('ascii'), validate=True)
            except (UnicodeEncodeError, ValueError):
                return None, 'Browser SSH public key is invalid.'
            if len(public_key_bytes) != 32:
                return None, 'Browser SSH public key must be 32 bytes.'
            browser_key = {
                'profile_id': profile_id,
                'key_id': key_id,
                'public_key': public_key,
                'fingerprint': hashlib.sha256(public_key_bytes).hexdigest(),
            }

        return {
            'host': host,
            'port': port,
            'username': user,
            'password': password,
            'profile_name': profile_name or None,
            'browser_key': browser_key,
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        bridge = self._bridge_cls(session_token, terminal_id, **self._bridge_kwargs)
        profile_name = payload.get('profile_name')
        if profile_name:
            bridge.terminal_label = f'SSH - {profile_name}'
        return bridge

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(
            payload['host'],
            payload['port'],
            payload['username'],
            payload['password'],
            browser_key=payload.get('browser_key'),
            cols=cols,
            rows=rows,
        )

    def prepare_connection_failure(self, sid, bridge, payload, result):
        failure = super().prepare_connection_failure(sid, bridge, payload, result)
        action_type = None
        action_message = None
        action_question = None

        if isinstance(result, dict):
            action_type = result.get('action_type')
            action_message = result.get('action_message')
            action_question = result.get('action_question')
        if action_type not in self._allowed_action_types:
            action_type = None
            action_message = None
            action_question = None

        action_id = None
        if action_type == 'offer_localhost_key_setup':
            action = bridge.prepare_backend_action(
                action_type,
                payload,
                expires_at=self._time_func() + self._key_setup_ttl_seconds,
                message=action_message,
                question=action_question,
            )
            if action:
                action_id = self._token_urlsafe(16)
                self._backend_action_store.set(sid, action_id, action)
            else:
                action_type = None
                action_message = None
                action_question = None

        failure.update({
            'action_type': action_type,
            'action_message': action_message,
            'action_question': action_question,
            'action_id': action_id,
        })
        return failure

    def execute_backend_action(self, action):
        if action.action_type != 'offer_localhost_key_setup':
            return super().execute_backend_action(action)
        return self._bridge_cls.execute_backend_action(action, **self._bridge_kwargs)
