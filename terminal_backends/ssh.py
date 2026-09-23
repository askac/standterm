import base64
import codecs
import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from pathlib import Path

from .base import BackendAction, BackendSettingSchema, BackendStartFieldSchema, TerminalBackendPlugin, TerminalBridge
from .ssh_host_keys import (
    HOST_KEY_ACTION_TYPES, ConfirmHostKeyPolicy, HostKeyConfirmationRequired,
    SSHHostKeyStore, fingerprint, host_key_name,
)
from runtime_logging import log_message
from .windows_network import WindowsNetworkSocket, windows_network_executable


SSH_PROFILE_NAME_MAX_LENGTH = 64
SSH_BROWSER_KEY_ID_MAX_LENGTH = 128
SSH_BROWSER_KEY_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')
SSH_MAX_JUMP_HOSTS = 3
SSH_CONNECT_TIMEOUT_SECONDS = 15
SSH_AUTH_TIMEOUT_SECONDS = 45
SSH_FORWARD_TIMEOUT_SECONDS = 15
SSH_LOGIN_TIMEOUT_SECONDS = 180
SSH_LOGIN_PASSWORD_ATTEMPTS = 3
SSH_LOGIN_MAX_PASSWORD_BYTES = 4096
SSH_LOGIN_POLL_SECONDS = 0.25
SFTP_FILE_REFERENCE_TTL_SECONDS = 5 * 60
SFTP_FILE_REFERENCE_MAX_RECORDS = 4096
SFTP_FILE_REFERENCE_TOKEN_BYTES = 12
SFTP_IO_TIMEOUT_SECONDS = 60
SSH_READ_CHUNK_BYTES = 4096
SSH_READ_BATCH_CHUNKS = 16
SSH_READ_BATCH_SECONDS = 0.002
SSH_READ_IDLE_SECONDS = 0.01


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
        known_hosts_path=None,
    ):
        super().__init__(owner_session, terminal_id)
        self._get_paramiko = get_paramiko
        self._ssh_term = ssh_term
        self._local_public_key_types = local_public_key_types
        self._request_browser_signature = request_browser_signature
        self._host_key_store = SSHHostKeyStore(get_paramiko(), known_hosts_path)
        self._host_key_snapshot = None
        self._pending_host_key = None
        self._browser_signer_sid = None
        self._connection_resources = []
        self._connection_lock = threading.RLock()
        self._connection_cancelled = threading.Event()
        self._connection_node = None
        self._login_request = None
        self.attempt_id = None
        self._sftp_lock = threading.Lock()
        self._sftp_file_refs_lock = threading.Lock()
        self._sftp_file_refs = {}
        self._sftp_endpoint = None
        self.ssh = None
        self.auth_method = None
        self.network_origin = 'core'
        self._reset_ssh_client()
        self.channel = None
        self._output_decoder = codecs.getincrementaldecoder('utf-8')(errors='ignore')

    def metadata(self, cols=None, rows=None):
        metadata = super().metadata(cols=cols, rows=rows)
        if self.auth_method:
            metadata['auth_method'] = self.auth_method
        if self._sftp_endpoint:
            metadata['ssh_target'] = {
                'host': self._sftp_endpoint['host'],
                'port': self._sftp_endpoint['port'],
                'username': self._sftp_endpoint['user'],
            }
            if self.network_origin == 'windows':
                metadata['ssh_target']['network_origin'] = 'windows'
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

    def _reset_ssh_client(self, trust_unknown_host=False, interactive_node=None, local_direct=False):
        if self._connection_cancelled.is_set():
            raise RuntimeError('SSH connection cancelled.')
        paramiko_module = self._get_paramiko()
        if self.ssh:
            self.ssh.close()
        self.ssh = paramiko_module.SSHClient()
        if interactive_node is not None:
            bridge = self

            class LoginHostKeyPolicy(paramiko_module.MissingHostKeyPolicy):
                def missing_host_key(self, client, hostname, key):
                    saved = bridge._host_key_snapshot['keys']
                    if any(item == key for item in saved) or (local_direct and not saved):
                        return
                    hint = bridge._host_key_confirmation_hint(key)
                    answer = bridge._request_login_input(interactive_node, 'host_key',
                                                        message=hint['action_message'],
                                                        question=hint['action_question'])
                    if answer is not True:
                        raise RuntimeError('SSH host key was not accepted.')
                    with bridge._connection_lock:
                        bridge._check_connection_active()
                        bridge._host_key_store.update(bridge._host_key_snapshot, key)
                    bridge._pending_host_key = None

            # Verify both unknown and changed keys in the policy, before authentication.
            self.ssh.set_missing_host_key_policy(LoginHostKeyPolicy())
            return
        if self._host_key_snapshot is not None:
            for key in self._host_key_snapshot['keys']:
                keys = self.ssh.get_host_keys()
                target = self._host_key_snapshot['host_key_name']
                if not keys.lookup(target) or key.get_name() not in keys.lookup(target):
                    keys.add(target, key.get_name(), key)
        if trust_unknown_host:
            self.ssh.set_missing_host_key_policy(paramiko_module.AutoAddPolicy())
        else:
            self.ssh.set_missing_host_key_policy(ConfirmHostKeyPolicy())

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
        if action_type == 'confirm_ssh_host_key' and self._pending_host_key is not None:
            return BackendAction(
                action_type=action_type,
                terminal_id=payload['terminal_id'],
                metadata={'snapshot': self._host_key_snapshot, 'key': self._pending_host_key,
                          'attempt_id': self.attempt_id},
                expires_at=expires_at,
                message=message,
                question=question,
            )
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
                'attempt_id': self.attempt_id,
                'key_entry': missing_entries[0],
            },
            expires_at=expires_at,
            message=message,
            question=question,
        )

    @classmethod
    def execute_backend_action(cls, action, **bridge_kwargs):
        if action.action_type in HOST_KEY_ACTION_TYPES:
            try:
                store = SSHHostKeyStore(bridge_kwargs['get_paramiko'](), bridge_kwargs.get('known_hosts_path'))
                store.update(action.metadata['snapshot'], action.metadata.get('key'))
                return {
                    'status': 'success',
                    'message': ('SSH host key forgotten.' if action.action_type == 'forget_ssh_host_key'
                                else 'SSH host key saved. Connect again to continue.'),
                }
            except (OSError, ValueError) as exc:
                return {'status': 'failed', 'message': str(exc), 'error_code': 'ssh_host_key_update_failed'}
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
            if isinstance(exc, getattr(paramiko_module, 'BadHostKeyException', ())):
                raise
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
                if isinstance(exc, getattr(paramiko_module, 'BadHostKeyException', ())):
                    raise
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

    def _own_connection_resource(self, resource):
        with self._connection_lock:
            if self._connection_cancelled.is_set():
                resource.close()
                raise RuntimeError('SSH connection cancelled.')
            self._connection_resources.append(resource)
        return resource

    def _check_connection_active(self):
        if self._connection_cancelled.is_set():
            raise RuntimeError('SSH connection cancelled.')

    def _request_login_input(self, node, kind, **details):
        with self._connection_lock:
            self._check_connection_active()
            payload = {
                'message_type': 'ssh_login_prompt', 'attempt_id': self.attempt_id,
                **self._connection_node, 'node_id': node['node_id'], 'kind': kind, 'phase': kind,
                'request_id': secrets.token_urlsafe(24), 'timeout_seconds': SSH_LOGIN_TIMEOUT_SECONDS,
                'host': node['host'], 'port': node['port'], 'username': node['username'], **details,
            }
            pending = {'payload': payload, 'event': threading.Event(), 'answer': None, 'answered': False,
                       'deadline': time.monotonic() + SSH_LOGIN_TIMEOUT_SECONDS}
            self._login_request = pending
            self._connection_node['phase'] = kind
        try:
            self.emit_output(payload)
            while not pending['event'].wait(min(SSH_LOGIN_POLL_SECONDS, max(0, pending['deadline'] - time.monotonic()))):
                self._check_connection_active()
                if time.monotonic() >= pending['deadline']:
                    raise RuntimeError('SSH login input timed out.')
                transport = self.ssh.get_transport() if self.ssh else None
                if transport is not None and not transport.is_active():
                    raise RuntimeError('SSH server closed the connection while waiting for login input.')
            with self._connection_lock:
                self._check_connection_active()
                if not pending['answered']:
                    raise RuntimeError('SSH login input was cancelled.')
                return pending['answer']
        finally:
            with self._connection_lock:
                if self._login_request is pending:
                    self._login_request = None
                pending['answer'] = None

    def resolve_login_input(self, sid, data):
        with self._connection_lock:
            pending = self._login_request
            if (not pending or pending['answered'] or time.monotonic() >= pending['deadline'] or self._connection_cancelled.is_set()
                    or sid != self._browser_signer_sid or not isinstance(data, dict)):
                return False
            expected = pending['payload']
            if data.get('terminal_id') != self.terminal_id or any(
                    data.get(field) != expected[field] for field in ('attempt_id', 'node_id', 'request_id', 'kind')):
                return False
            if expected['kind'] == 'password':
                answer = data.get('password')
                if not isinstance(answer, str) or len(answer.encode('utf-8')) > SSH_LOGIN_MAX_PASSWORD_BYTES:
                    return False
            else:
                answer = data.get('accept')
                if not isinstance(answer, bool):
                    return False
            pending.update(answer=answer, answered=True)
            pending['event'].set()
            return True

    def _login_auth_strategy(self, node, index, total, pkey):
        paramiko_module = self._get_paramiko()
        bridge = self

        class LoginAuthStrategy(paramiko_module.AuthStrategy):
            def authenticate(self, transport):
                if pkey:
                    bridge._connection_progress(node, index, total, 'authenticate')
                    transport.auth_publickey(node['username'], pkey)
                else:
                    for attempt in range(SSH_LOGIN_PASSWORD_ATTEMPTS):
                        password = node.get('password') if attempt == 0 else None
                        if not password:
                            password = bridge._request_login_input(
                                node, 'password', message='Password was rejected. Try again.' if attempt else '')
                        try:
                            bridge._connection_progress(node, index, total, 'authenticate')
                            transport.auth_password(node['username'], password)
                            break
                        except paramiko_module.BadAuthenticationType as exc:
                            if 'keyboard-interactive' in exc.allowed_types:
                                raise RuntimeError('This server requires additional interactive SSH authentication that this login card does not support.') from exc
                            raise RuntimeError('This SSH server does not accept password authentication. Choose a key.')
                        except paramiko_module.AuthenticationException:
                            if attempt == SSH_LOGIN_PASSWORD_ATTEMPTS - 1 or not transport.is_active():
                                raise
                        finally:
                            password = None
                bridge._check_connection_active()
                if not transport.is_authenticated():
                    raise RuntimeError('Additional SSH authentication is required and is not supported by this login card.')

        return LoginAuthStrategy(ssh_config=None)

    def _connection_progress(self, node, index, total, phase):
        if self._connection_cancelled.is_set():
            raise RuntimeError('SSH connection cancelled.')
        self._connection_node = {'node_id': node['node_id'], 'hop': index + 1,
                                 'total': total, 'phase': phase}
        self.emit_output({
            'message_type': 'ssh_connect_progress', 'attempt_id': self.attempt_id,
            **self._connection_node, 'host': node['host'], 'port': node['port'],
        })

    def _connect_route(self, route, cols, rows, interactive_login=False):
        paramiko_module = self._get_paramiko()
        previous = None
        try:
            for index, node in enumerate(route):
                self._connection_progress(node, index, len(route), 'connect')
                identity = node.get('host_key_alias') or node['host']
                self._host_key_snapshot = self._host_key_store.snapshot(identity, node['port'])
                self._pending_host_key = None
                if previous is None:
                    # Connect to the network address; the alias is only a trust identity.
                    if self.network_origin == 'windows':
                        sock = self._own_connection_resource(WindowsNetworkSocket())
                        sock.connect((node['host'], node['port']), timeout=SSH_CONNECT_TIMEOUT_SECONDS)
                    else:
                        sock = self._own_connection_resource(socket.create_connection(
                            (node['host'], node['port']), timeout=SSH_CONNECT_TIMEOUT_SECONDS))
                else:
                    self._connection_progress(node, index, len(route), 'forward')
                    sock = self._own_connection_resource(previous.get_transport().open_channel(
                        'direct-tcpip', (node['host'], node['port']), ('127.0.0.1', 0),
                        timeout=SSH_FORWARD_TIMEOUT_SECONDS))
                # Retain upstream clients until the complete route is closed.
                self.ssh = None
                self._reset_ssh_client(
                    interactive_node=node if interactive_login else None,
                    local_direct=self.network_origin == 'core' and len(route) == 1
                    and not node.get('host_key_alias') and self._is_local_target(node['host']))
                client = self._own_connection_resource(self.ssh)
                key = node.get('browser_key')
                pkey = None
                if key:
                    if not self._browser_signer_sid or not self._request_browser_signature:
                        raise BrowserSSHKeyError('Browser SSH signer is unavailable.')
                    pkey = BrowserEd25519Key(
                        paramiko_module, base64.b64decode(key['public_key']),
                        lambda data, algorithm, bound_key=key: self._request_browser_signature(
                            self, self._browser_signer_sid, bound_key, data, algorithm))
                self._connection_progress(node, index, len(route), 'verify_and_authenticate')
                auth_options = {'auth_strategy': self._login_auth_strategy(node, index, len(route), pkey)} if interactive_login else {
                    'password': None if key else node['password'], 'pkey': pkey, 'allow_agent': False, 'look_for_keys': False}
                client.connect(
                    identity, port=node['port'], username=node['username'], sock=sock,
                    timeout=SSH_CONNECT_TIMEOUT_SECONDS, banner_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
                    auth_timeout=SSH_AUTH_TIMEOUT_SECONDS, channel_timeout=SSH_FORWARD_TIMEOUT_SECONDS,
                    **auth_options)
                previous = client
                self.auth_method = 'browser-key' if key else 'password'
                self._connection_progress(node, index, len(route), 'authenticated')
            self._connection_progress(route[-1], len(route) - 1, len(route), 'shell')
            self.channel = self._own_connection_resource(self.ssh.invoke_shell(
                term=self._ssh_term, width=cols, height=rows))
            self.channel.setblocking(0)
            target = route[-1]
            self._sftp_endpoint = {
                'host': target['host'], 'port': target['port'], 'user': target['username'],
                'route': json.dumps([[n['host'].lower(), n['port'], n['username'],
                                      n.get('host_key_alias', '')] for n in route], separators=(',', ':')),
            }
            if self.network_origin == 'windows':
                self._sftp_endpoint['network_origin'] = 'windows'
            return True, None
        except Exception as exc:
            if isinstance(exc, (HostKeyConfirmationRequired, paramiko_module.BadHostKeyException)):
                result = self._host_key_confirmation_hint(exc.key)
            else:
                result = {'message': str(exc), 'error_code': 'ssh_route_failed'}
            context = self._connection_node or {}
            result['route_context'] = dict(context)
            result['message'] = f"Hop {context.get('hop', 1)}/{len(route)}: {result['message']}"
            return False, result

    def connect(self, host, port, user, password=None, browser_key=None, cols=80, rows=24,
                route=None, attempt_id=None, interactive_login=False, network_origin='core'):
        self.attempt_id = attempt_id
        if network_origin not in ('core', 'windows'):
            return False, {'message': 'Invalid SSH network origin.', 'error_code': 'ssh_network_origin_invalid'}
        self.network_origin = network_origin
        if network_origin == 'windows':
            # Windows localhost is not Core localhost: never offer local keys or automatic trust.
            route = route or [{'node_id': 'direct', 'host': host, 'port': port, 'username': user,
                               'password': password or '', 'browser_key': browser_key}]
            return self._connect_route(route, cols, rows, interactive_login=interactive_login)
        if interactive_login:
            node = route[0]
            if (len(route) == 1 and not node.get('host_key_alias') and self._is_local_target(node['host'])
                    and not node.get('browser_key') and not node.get('password')):
                self._connection_progress(node, 0, 1, 'local_keys')
                success, result = self.connect(host, port, user, route=route, attempt_id=attempt_id, cols=cols, rows=rows)
                if success or (isinstance(result, dict) and result.get('action_type') == 'offer_localhost_key_setup'):
                    if success:
                        self._connection_progress(node, 0, 1, 'authenticated')
                    elif isinstance(result, dict):
                        result['route_context'] = dict(self._connection_node)
                    return success, result
                if self.ssh:
                    self.ssh.close()
                    self.ssh = None
            return self._connect_route(route, cols, rows, interactive_login=True)
        if route and (len(route) > 1 or route[0].get('host_key_alias')):
            return self._connect_route(route, cols, rows)
        if route:
            node = route[0]
            host, port, user = node['host'], node['port'], node['username']
            password, browser_key = node['password'], node.get('browser_key')
        paramiko_module = self._get_paramiko()
        try:
            self._pending_host_key = None
            self._host_key_snapshot = self._host_key_store.snapshot(host, port)
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
        except HostKeyConfirmationRequired as exc:
            return False, self._host_key_confirmation_hint(exc.key)
        except BrowserSSHKeyError as exc:
            log_message(f"[!] Browser SSH key error: {exc}")
            return False, {
                'message': str(exc),
                'error_code': 'ssh_browser_key_failed',
            }
        except Exception as e:
            if isinstance(e, getattr(paramiko_module, 'BadHostKeyException', ())):
                return False, self._host_key_confirmation_hint(e.key)
            error_msg = str(e)
            log_message(f"[!] SSH Connection Error: {error_msg}")
            return False, {'message': error_msg}

    def _host_key_confirmation_hint(self, key):
        self._pending_host_key = key
        saved = self._host_key_snapshot['keys']
        target = self._host_key_snapshot['host_key_name']
        message = f'SSH host key {"changed" if saved else "is unknown"} for {target}.'
        details = [message]
        if saved:
            details.append('Saved: ' + '; '.join(fingerprint(item) for item in saved))
        details.append('Received: ' + fingerprint(key))
        details.append('Verify this fingerprint with the host administrator before trusting it.')
        details.append('This updates the Core account\'s known_hosts file, shared with other SSH clients.')
        if self.network_origin == 'windows':
            details.append('The first SSH hop uses Windows networking. Use a host key alias for a different host at the same address.')
        return {
            'message': message,
            'error_code': 'ssh_host_key_changed' if saved else 'ssh_host_key_unknown',
            'action_type': 'confirm_ssh_host_key',
            'action_message': '\n'.join(details),
            'action_question': ('Replace the saved keys for this host and port?' if saved
                                else 'Remember this host key?'),
        }

    def read_loop(self):
        log_message(f"[*] Starting SSH read loop for {self.sid}")
        while True:
            # Yield between bounded batches; only idle channels need a timed wait.
            self.runtime.sleep(0)
            if not self.channel:
                break

            try:
                chunks = []
                eof = False
                deadline = time.monotonic() + SSH_READ_BATCH_SECONDS
                try:
                    for _ in range(SSH_READ_BATCH_CHUNKS):
                        if not self.channel.recv_ready():
                            break
                        chunk = self.channel.recv(SSH_READ_CHUNK_BYTES)
                        if not chunk:
                            eof = True
                            break
                        chunks.append(chunk)
                        if time.monotonic() >= deadline:
                            break
                finally:
                    # Preserve data read before EOF or a later read failure.
                    data = self._output_decoder.decode(b''.join(chunks))
                    if data:
                        self.emit_output({
                            'message_type': 'terminal',
                            'data': data,
                        })

                if eof or (self.channel.exit_status_ready() and not self.channel.recv_ready()):
                    log_message(f"[*] SSH session exited for {self.sid}")
                    self.emit_output({
                        'message_type': 'ssh_closed',
                        'message': 'SSH session closed.',
                    })
                    break
                if not chunks:
                    self.runtime.sleep(SSH_READ_IDLE_SECONDS)
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

    def cancel_connection(self):
        self._connection_cancelled.set()

    def close(self):
        self.cancel_connection()
        with self._connection_lock:
            if self._login_request:
                self._login_request['answer'] = None
                self._login_request['event'].set()
            resources, self._connection_resources = self._connection_resources, []
        for resource in reversed(resources):
            try:
                resource.close()
            except Exception:
                pass
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
        fields = [
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
        if windows_network_executable():
            fields.append(BackendStartFieldSchema(
                name='network_origin', label='Connect from', value_type='string', input_type='select',
                default_value='core', options=({'value': 'core', 'label': 'Core (WSL)'},
                                               {'value': 'windows', 'label': 'Windows (preview)'}),
            ))
        return fields

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

        network_origin = data.get('network_origin', 'core')
        if network_origin not in ('core', 'windows'):
            return None, 'Invalid SSH network origin.'
        if network_origin == 'windows' and not windows_network_executable():
            return None, {'error_code': 'ssh_windows_network_unavailable',
                          'message': 'Windows network access requires WSL interoperability and Windows PowerShell on PATH.'}

        if 'route' in data:
            route = data['route']
            interactive_login = data.get('interactive_login', False)
            if not isinstance(interactive_login, bool):
                return None, 'SSH interactive login flag is invalid.'
            attempt_id = data.get('attempt_id')
            if (not isinstance(attempt_id, str) or len(attempt_id) > SSH_BROWSER_KEY_ID_MAX_LENGTH
                    or not SSH_BROWSER_KEY_ID_PATTERN.fullmatch(attempt_id)):
                return None, 'SSH connection attempt id is invalid.'
            if not isinstance(route, list) or not 1 <= len(route) <= SSH_MAX_JUMP_HOSTS + 1:
                return None, f'SSH routes support at most {SSH_MAX_JUMP_HOSTS} jump hosts plus the target.'
            nodes, seen = [], set()
            for index, raw in enumerate(route):
                if not isinstance(raw, dict) or 'route' in raw or 'network_origin' in raw:
                    return None, 'SSH route node is invalid.'
                if not all(field in raw for field in ('host', 'port', 'username')):
                    return None, 'Each SSH route node requires its host, port and username.'
                node_id = raw.get('node_id')
                if (not isinstance(node_id, str) or len(node_id) > SSH_BROWSER_KEY_ID_MAX_LENGTH
                        or not SSH_BROWSER_KEY_ID_PATTERN.fullmatch(node_id) or node_id in seen):
                    return None, 'SSH route has an invalid or repeated node id.'
                seen.add(node_id)
                node, error = self.validate_start_payload(
                    raw, terminal_id, client_ip, browser_authorized, context)
                if error:
                    return None, error
                node['node_id'] = node_id
                if node['browser_key']:
                    node['browser_key'].update(node_id=node_id, attempt_id=attempt_id)
                nodes.append(node)
            payload = dict(nodes[-1])
            name = data.get('profile_name')
            if name is not None and (not isinstance(name, str) or len(name) > SSH_PROFILE_NAME_MAX_LENGTH
                                     or self._has_control_chars(name)):
                return None, 'SSH entry name is invalid.'
            payload.update(route=nodes, attempt_id=attempt_id, profile_name=name or None, interactive_login=interactive_login)
            if network_origin == 'windows':
                payload['network_origin'] = network_origin
            return payload, None

        host = data.get('host', self._get_default_host(context=context))
        if not isinstance(host, str):
            return None, 'Host must be a string.'
        host = host.strip()
        if not host or len(host) > self._max_host_length:
            return None, 'Host is empty or too long.'
        if self._has_control_chars(host):
            return None, 'Host contains invalid control characters.'
        try:
            host_key_name(host, 22)
        except ValueError as exc:
            return None, str(exc)
        alias = data.get('host_key_alias') or ''
        if not isinstance(alias, str) or len(alias) > self._max_host_length:
            return None, 'SSH host key alias is invalid.'
        alias = alias.strip()
        if alias:
            try:
                host_key_name(alias, 22)
            except ValueError as exc:
                return None, str(exc)

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
            owner_field = 'credential_id' if data.get('credential_id') is not None else 'profile_id'
            if owner_field == 'credential_id' and data.get('profile_id') is not None:
                return None, 'Choose either an SSH profile key or a browser credential.'
            owner_id = data.get(owner_field)
            key_id = data.get('key_id')
            for field_name, field_value in (('SSH key owner id', owner_id), ('SSH key id', key_id)):
                if (
                    not isinstance(field_value, str)
                    or not field_value
                    or len(field_value) > SSH_BROWSER_KEY_ID_MAX_LENGTH
                    or not SSH_BROWSER_KEY_ID_PATTERN.fullmatch(field_value)
                ):
                    return None, f'{field_name} is invalid.'
            if owner_field == 'credential_id' and owner_id != key_id:
                return None, 'SSH credential id must match its key id.'
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
                owner_field: owner_id,
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
            'host_key_alias': alias,
            **({'network_origin': network_origin} if network_origin == 'windows' else {}),
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        bridge = self._bridge_cls(session_token, terminal_id, **self._bridge_kwargs)
        profile_name = payload.get('profile_name')
        if profile_name:
            bridge.terminal_label = f'SSH - {profile_name}'
        return bridge

    def connect_bridge(self, bridge, payload, cols, rows):
        options = {}
        if payload.get('network_origin') == 'windows':
            options['network_origin'] = 'windows'
        if payload.get('route'):
            options.update(route=payload['route'], attempt_id=payload['attempt_id'])
            if payload.get('interactive_login'):
                options['interactive_login'] = True
        elif payload.get('host_key_alias'):
            options.update(route=[{**payload, 'node_id': 'direct'}])
        return bridge.connect(
            payload['host'],
            payload['port'],
            payload['username'],
            payload['password'],
            browser_key=payload.get('browser_key'),
            cols=cols,
            rows=rows,
            **options,
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
        if action_type in {'offer_localhost_key_setup', 'confirm_ssh_host_key'}:
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
        if action.action_type not in {'offer_localhost_key_setup', *HOST_KEY_ACTION_TYPES}:
            return super().execute_backend_action(action)
        return self._bridge_cls.execute_backend_action(action, **self._bridge_kwargs)

    def prepare_host_key_forget(self, sid, payload):
        store = SSHHostKeyStore(self._bridge_kwargs['get_paramiko'](), self._bridge_kwargs.get('known_hosts_path'))
        snapshot = store.snapshot(payload.get('host_key_alias') or payload['host'], payload['port'])
        if not snapshot['keys']:
            return None, None
        action = BackendAction(
            action_type='forget_ssh_host_key',
            terminal_id=payload['terminal_id'],
            metadata={'snapshot': snapshot},
            expires_at=self._time_func() + self._key_setup_ttl_seconds,
            message=(f"Saved SSH host keys for {snapshot['host_key_name']}:\n"
                     + '\n'.join(fingerprint(key) for key in snapshot['keys'])
                     + '\nThis also affects other SSH clients using this known_hosts file.'),
            question='Forget these keys? Existing connections will remain open.',
        )
        action_id = self._token_urlsafe(16)
        self._backend_action_store.set(sid, action_id, action)
        return action_id, action

    def inspect_host_key(self, payload):
        store = SSHHostKeyStore(self._bridge_kwargs['get_paramiko'](), self._bridge_kwargs.get('known_hosts_path'))
        snapshot = store.snapshot(payload.get('host_key_alias') or payload['host'], payload['port'])
        return {'identity': snapshot['host_key_name'], 'fingerprints': [fingerprint(key) for key in snapshot['keys']]}
