#!/usr/bin/env python3
import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error

import agent_cli


FRAME_PREFIX = 'STFT1'
SAFE_VALUE_RE = re.compile(r'^[A-Za-z0-9_.:@%+=,-]*$')
PLACEHOLDER_RE = re.compile(r'\{([A-Za-z0-9_]+)\}')
FRAME_RE = re.compile(r'^STFT1:([A-Za-z0-9_-]+):([0-9]+):(OK|CHUNK|DONE|ERR):([A-Za-z0-9_-]+)$')
NONCE_BYTES = 12
DEFAULT_MAX_COMMAND_BYTES = 3000
DEFAULT_MAX_MARKER_BYTES = 3500
DEFAULT_WAIT_MS = 10000
DEFAULT_SETTLE_MS = 100
DEFAULT_GET_CHUNK_BYTES = 1024
FREEBSD_TCSH_PYTHON_CANDIDATES = (
    '/usr/local/bin/python3.13',
    '/usr/local/bin/python3.12',
    '/usr/local/bin/python3.11',
    '/usr/local/bin/python3.10',
    '/usr/local/bin/python3.9',
    '/usr/local/bin/python3.8',
    '/usr/local/bin/python3.7',
    '/usr/local/bin/python3',
    'python3',
)

COMMON_PYTHON_COMMANDS = {
    'probe': {
        'expect': 'OK',
        'template': (
            'python3 -c \'import base64,json,sys;'
            'n=sys.argv[1];s=sys.argv[2];'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'print("STFT1:%s:%s:OK:%s"%(n,s,e(dict(cmd="probe",python=sys.version.split()[0]))))\' '
            '{nonce} {seq}'
        ),
    },
    'init_put': {
        'expect': 'OK',
        'template': (
            'python3 -c \'import base64,json,os,sys;'
            'n,s,pb,ow=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]=="1";'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'm=lambda k,o:print("STFT1:%s:%s:%s:%s"%(n,s,k,e(o)));'
            'p=base64.urlsafe_b64decode(pb+"="*((4-len(pb)%4)%4)).decode();'
            'td=os.path.join(os.path.dirname(p) or ".",".standterm-transfer");'
            'bf=os.path.join(td,n+".b64url");pt=p+".part";'
            'try:\n'
            '    os.makedirs(td,exist_ok=True)\n'
            '    (not os.path.exists(p) or ow) or (_ for _ in ()).throw(RuntimeError("remote_exists"))\n'
            '    open(bf,"wb").close();m("OK",dict(cmd="init_put",temp_b64=bf,part=pt))\n'
            'except Exception as x:\n'
            '    m("ERR",dict(cmd="init_put",error_code=str(x)))\' '
            '{nonce} {seq} {rfile_b64url} {overwrite_flag}'
        ),
    },
    'write_chunk': {
        'expect': 'OK',
        'template': (
            'python3 -c \'import base64,json,os,sys;'
            'n,s,pb,off,ch=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),sys.argv[5];'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'm=lambda k,o:print("STFT1:%s:%s:%s:%s"%(n,s,k,e(o)));'
            'p=base64.urlsafe_b64decode(pb+"="*((4-len(pb)%4)%4)).decode();'
            'bf=os.path.join(os.path.dirname(p) or ".",".standterm-transfer",n+".b64url");'
            'try:\n'
            '    cur=os.path.getsize(bf) if os.path.exists(bf) else 0\n'
            '    cur==off or (_ for _ in ()).throw(RuntimeError("offset_mismatch:%s"%cur))\n'
            '    open(bf,"ab").write(ch.encode("ascii"));m("OK",dict(cmd="write_chunk",offset=off,length=len(ch)))\n'
            'except Exception as x:\n'
            '    m("ERR",dict(cmd="write_chunk",offset=off,error_code=str(x)))\' '
            '{nonce} {seq} {rfile_b64url} {offset} {chunk_b64url}'
        ),
    },
    'finish_put': {
        'expect': 'DONE',
        'template': (
            'python3 -c \'import base64,hashlib,json,os,sys;'
            'n,s,pb,sz,sha,ow=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),sys.argv[5],sys.argv[6]=="1";'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'm=lambda k,o:print("STFT1:%s:%s:%s:%s"%(n,s,k,e(o)));'
            'p=base64.urlsafe_b64decode(pb+"="*((4-len(pb)%4)%4)).decode();'
            'td=os.path.join(os.path.dirname(p) or ".",".standterm-transfer");bf=os.path.join(td,n+".b64url");pt=p+".part";'
            'try:\n'
            '    b=open(bf,"rb").read();data=base64.urlsafe_b64decode(b+b"="*((4-len(b)%4)%4))\n'
            '    got=hashlib.sha256(data).hexdigest();len(data)==sz or (_ for _ in ()).throw(RuntimeError("size_mismatch"))\n'
            '    got==sha or (_ for _ in ()).throw(RuntimeError("sha256_mismatch"))\n'
            '    (not os.path.exists(p) or ow) or (_ for _ in ()).throw(RuntimeError("remote_exists"))\n'
            '    open(pt,"wb").write(data);os.replace(pt,p);m("DONE",dict(cmd="finish_put",bytes=sz,sha256=got))\n'
            'except Exception as x:\n'
            '    m("ERR",dict(cmd="finish_put",error_code=str(x)))\' '
            '{nonce} {seq} {rfile_b64url} {expected_size} {expected_sha256} {overwrite_flag}'
        ),
    },
    'stat_hash': {
        'expect': 'DONE',
        'template': (
            'python3 -c \'import base64,hashlib,json,os,sys;'
            'n,s,pb=sys.argv[1],sys.argv[2],sys.argv[3];'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'm=lambda k,o:print("STFT1:%s:%s:%s:%s"%(n,s,k,e(o)));'
            'p=base64.urlsafe_b64decode(pb+"="*((4-len(pb)%4)%4)).decode();'
            'try:\n'
            '    data=open(p,"rb").read();m("DONE",dict(cmd="stat_hash",bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))\n'
            'except Exception as x:\n'
            '    m("ERR",dict(cmd="stat_hash",error_code=str(x)))\' '
            '{nonce} {seq} {rfile_b64url}'
        ),
    },
    'read_chunk': {
        'expect': 'CHUNK',
        'template': (
            'python3 -c \'import base64,json,sys;'
            'n,s,pb,off,ln=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),int(sys.argv[5]);'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'm=lambda k,o:print("STFT1:%s:%s:%s:%s"%(n,s,k,e(o)));'
            'p=base64.urlsafe_b64decode(pb+"="*((4-len(pb)%4)%4)).decode();'
            'try:\n'
            '    f=open(p,"rb");f.seek(off);data=f.read(ln);m("CHUNK",dict(cmd="read_chunk",offset=off,length=len(data),data_b64url=base64.urlsafe_b64encode(data).decode().rstrip("=")))\n'
            'except Exception as x:\n'
            '    m("ERR",dict(cmd="read_chunk",offset=off,error_code=str(x)))\' '
            '{nonce} {seq} {rfile_b64url} {offset} {length}'
        ),
    },
    'cleanup': {
        'expect': 'OK',
        'best_effort': True,
        'template': (
            'python3 -c \'import base64,json,os,sys;'
            'n,s,pb=sys.argv[1],sys.argv[2],sys.argv[3];'
            'e=lambda o:base64.urlsafe_b64encode(json.dumps(o,separators=(",",":")).encode()).decode().rstrip("=");'
            'm=lambda k,o:print("STFT1:%s:%s:%s:%s"%(n,s,k,e(o)));'
            'p=base64.urlsafe_b64decode(pb+"="*((4-len(pb)%4)%4)).decode();'
            'td=os.path.join(os.path.dirname(p) or ".",".standterm-transfer");'
            'bf=os.path.join(td,n+".b64url");pt=p+".part";'
            'ok=[]\n'
            'for q in (bf,pt):\n'
            '    try:\n'
            '        os.path.exists(q) and os.unlink(q);ok.append(q)\n'
            '    except Exception: pass\n'
            'm("OK",dict(cmd="cleanup",removed=ok))\' '
            '{nonce} {seq} {rfile_b64url}'
        ),
    },
}


def python_commands_for(executable, python2=False):
    commands = copy.deepcopy(COMMON_PYTHON_COMMANDS)
    for spec in commands.values():
        spec['template'] = spec['template'].replace('python3 -c', executable + ' -c')
        spec['template'] = spec['template'].replace('.decode();', '.decode("utf-8");')
        spec['template'] = spec['template'].replace(';try:\n', '\ntry:\n')
        if python2:
            spec['template'] = spec['template'].replace(
                '    os.makedirs(td,exist_ok=True)\n',
                '    (os.path.isdir(td) or os.makedirs(td))\n',
            )
            spec['template'] = spec['template'].replace('os.replace(pt,p)', 'os.rename(pt,p)')
        prefix = executable + " -c '"
        marker = "' "
        start = spec['template'].find(prefix)
        end = spec['template'].rfind(marker)
        if start >= 0 and end > start:
            code_start = start + len(prefix)
            code = spec['template'][code_start:end]
            args = spec['template'][end + 1:]
            spec['template'] = executable + " -c 'exec(" + json.dumps(code) + ")'" + args
    return commands


COMMON_PYTHON3_COMMANDS = python_commands_for('python3')
COMMON_PYTHON2_COMMANDS = python_commands_for('python2', python2=True)

WINDOWS_POWERSHELL_COMMANDS = {
    'probe': {
        'expect': 'OK',
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$j=$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'probe\'+$Q+\',\'+$Q+\'powershell\'+$Q+\':true\'+$RB;'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':OK:\'+$p)"'
        ),
    },
    'init_put': {
        'expect': 'OK',
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$pb=\'{rfile_b64url}\';$ow=\'{overwrite_flag}\' -eq \'1\';'
            '$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$pad=\'=\'*((4-$pb.Length%4)%4);$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($pb+$pad).Replace(\'-\',\'+\').Replace(\'_\',\'/\')));'
            '$dir=[IO.Path]::GetDirectoryName($path);if([string]::IsNullOrEmpty($dir)){{$dir=\'.\'}};'
            '$td=[IO.Path]::Combine($dir,\'.standterm-transfer\');$bf=[IO.Path]::Combine($td,$n+\'.b64url\');'
            '$kind=\'OK\';$code=\'\';'
            'try{{[IO.Directory]::CreateDirectory($td)|Out-Null;if((Test-Path -LiteralPath $path) -and -not $ow){{throw \'remote_exists\'}};[IO.File]::WriteAllBytes($bf,[byte[]]@())}}catch{{$kind=\'ERR\';$code=$_.Exception.Message}};'
            '$j=if($kind -eq \'OK\'){{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'init_put\'+$Q+$RB}}else{{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'init_put\'+$Q+\',\'+$Q+\'error_code\'+$Q+\':\'+$Q+$code+$Q+$RB}};'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':\'+$kind+\':\'+$p)"'
        ),
    },
    'write_chunk': {
        'expect': 'OK',
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$pb=\'{rfile_b64url}\';$off=[int64]\'{offset}\';$ch=\'{chunk_b64url}\';'
            '$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$pad=\'=\'*((4-$pb.Length%4)%4);$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($pb+$pad).Replace(\'-\',\'+\').Replace(\'_\',\'/\')));'
            '$dir=[IO.Path]::GetDirectoryName($path);if([string]::IsNullOrEmpty($dir)){{$dir=\'.\'}};'
            '$bf=[IO.Path]::Combine([IO.Path]::Combine($dir,\'.standterm-transfer\'),$n+\'.b64url\');'
            '$kind=\'OK\';$code=\'\';$cur=if(Test-Path -LiteralPath $bf){{(Get-Item -LiteralPath $bf).Length}}else{{0}};'
            'try{{if($cur -ne $off){{throw (\'offset_mismatch:\'+$cur)}};[IO.File]::AppendAllText($bf,$ch,[Text.Encoding]::ASCII)}}catch{{$kind=\'ERR\';$code=$_.Exception.Message}};'
            '$j=if($kind -eq \'OK\'){{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'write_chunk\'+$Q+\',\'+$Q+\'offset\'+$Q+\':\'+$off+\',\'+$Q+\'length\'+$Q+\':\'+$ch.Length+$RB}}else{{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'write_chunk\'+$Q+\',\'+$Q+\'offset\'+$Q+\':\'+$off+\',\'+$Q+\'error_code\'+$Q+\':\'+$Q+$code+$Q+$RB}};'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':\'+$kind+\':\'+$p)"'
        ),
    },
    'finish_put': {
        'expect': 'DONE',
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$pb=\'{rfile_b64url}\';$esz=[int64]\'{expected_size}\';$esha=\'{expected_sha256}\';$ow=\'{overwrite_flag}\' -eq \'1\';'
            '$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$pad=\'=\'*((4-$pb.Length%4)%4);$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($pb+$pad).Replace(\'-\',\'+\').Replace(\'_\',\'/\')));'
            '$dir=[IO.Path]::GetDirectoryName($path);if([string]::IsNullOrEmpty($dir)){{$dir=\'.\'}};'
            '$bf=[IO.Path]::Combine([IO.Path]::Combine($dir,\'.standterm-transfer\'),$n+\'.b64url\');$pt=$path+\'.part\';'
            '$kind=\'DONE\';$code=\'\';$got=\'\';$sz=0;'
            'try{{$b=[IO.File]::ReadAllText($bf,[Text.Encoding]::ASCII);$pad2=\'=\'*((4-$b.Length%4)%4);$bytes=[Convert]::FromBase64String(($b+$pad2).Replace(\'-\',\'+\').Replace(\'_\',\'/\'));'
            '$sz=$bytes.Length;$got=([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))).Replace(\'-\',\'\').ToLowerInvariant();'
            'if($sz -ne $esz){{throw \'size_mismatch\'}};if($got -ne $esha){{throw \'sha256_mismatch\'}};if((Test-Path -LiteralPath $path) -and -not $ow){{throw \'remote_exists\'}};'
            '[IO.File]::WriteAllBytes($pt,$bytes);Move-Item -LiteralPath $pt -Destination $path -Force}}catch{{$kind=\'ERR\';$code=$_.Exception.Message}};'
            '$j=if($kind -eq \'DONE\'){{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'finish_put\'+$Q+\',\'+$Q+\'bytes\'+$Q+\':\'+$sz+\',\'+$Q+\'sha256\'+$Q+\':\'+$Q+$got+$Q+$RB}}else{{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'finish_put\'+$Q+\',\'+$Q+\'error_code\'+$Q+\':\'+$Q+$code+$Q+$RB}};'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':\'+$kind+\':\'+$p)"'
        ),
    },
    'stat_hash': {
        'expect': 'DONE',
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$pb=\'{rfile_b64url}\';$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$pad=\'=\'*((4-$pb.Length%4)%4);$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($pb+$pad).Replace(\'-\',\'+\').Replace(\'_\',\'/\')));'
            '$kind=\'DONE\';$code=\'\';$got=\'\';$sz=0;'
            'try{{$bytes=[IO.File]::ReadAllBytes($path);$sz=$bytes.Length;$got=([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))).Replace(\'-\',\'\').ToLowerInvariant()}}catch{{$kind=\'ERR\';$code=$_.Exception.Message}};'
            '$j=if($kind -eq \'DONE\'){{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'stat_hash\'+$Q+\',\'+$Q+\'bytes\'+$Q+\':\'+$sz+\',\'+$Q+\'sha256\'+$Q+\':\'+$Q+$got+$Q+$RB}}else{{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'stat_hash\'+$Q+\',\'+$Q+\'error_code\'+$Q+\':\'+$Q+$code+$Q+$RB}};'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':\'+$kind+\':\'+$p)"'
        ),
    },
    'read_chunk': {
        'expect': 'CHUNK',
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$pb=\'{rfile_b64url}\';$off=[int64]\'{offset}\';$ln=[int]\'{length}\';$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$pad=\'=\'*((4-$pb.Length%4)%4);$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($pb+$pad).Replace(\'-\',\'+\').Replace(\'_\',\'/\')));'
            '$kind=\'CHUNK\';$code=\'\';$data=\'\';$got=0;'
            'try{{$fs=[IO.File]::OpenRead($path);$fs.Seek($off,[IO.SeekOrigin]::Begin)|Out-Null;$buf=New-Object byte[] $ln;$got=$fs.Read($buf,0,$ln);$fs.Close();if($got -lt $ln){{$buf=$buf[0..($got-1)]}};$data=[Convert]::ToBase64String($buf).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\')}}catch{{$kind=\'ERR\';$code=$_.Exception.Message}};'
            '$j=if($kind -eq \'CHUNK\'){{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'read_chunk\'+$Q+\',\'+$Q+\'offset\'+$Q+\':\'+$off+\',\'+$Q+\'length\'+$Q+\':\'+$got+\',\'+$Q+\'data_b64url\'+$Q+\':\'+$Q+$data+$Q+$RB}}else{{$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'read_chunk\'+$Q+\',\'+$Q+\'offset\'+$Q+\':\'+$off+\',\'+$Q+\'error_code\'+$Q+\':\'+$Q+$code+$Q+$RB}};'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':\'+$kind+\':\'+$p)"'
        ),
    },
    'cleanup': {
        'expect': 'OK',
        'best_effort': True,
        'template': (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            '$n=\'{nonce}\';$s=\'{seq}\';$pb=\'{rfile_b64url}\';$Q=[char]34;$LB=[char]123;$RB=[char]125;'
            '$pad=\'=\'*((4-$pb.Length%4)%4);$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($pb+$pad).Replace(\'-\',\'+\').Replace(\'_\',\'/\')));'
            '$dir=[IO.Path]::GetDirectoryName($path);if([string]::IsNullOrEmpty($dir)){{$dir=\'.\'}};'
            '$bf=[IO.Path]::Combine([IO.Path]::Combine($dir,\'.standterm-transfer\'),$n+\'.b64url\');$pt=$path+\'.part\';'
            'if(Test-Path -LiteralPath $bf){{Remove-Item -LiteralPath $bf -Force}};if(Test-Path -LiteralPath $pt){{Remove-Item -LiteralPath $pt -Force}};'
            '$j=$LB+$Q+\'cmd\'+$Q+\':\'+$Q+\'cleanup\'+$Q+$RB;'
            '$p=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)).TrimEnd(\'=\').Replace(\'+\',\'-\').Replace(\'/\',\'_\');'
            'Write-Output (\'STFT1:\'+$n+\':\'+$s+\':OK:\'+$p)"'
        ),
    },
}

BUILTIN_METHODS = {
    'builtin:freebsd-tcsh-python3': {
        'schema_version': 1,
        'name': 'builtin:freebsd-tcsh-python3',
        'shell': 'tcsh',
        'directions': ['put', 'get'],
        'requires': ['python3'],
        'newline': 'submit_after',
        'path_encoding': 'b64url_utf8',
        'chunk_encoding': 'b64url',
        'max_command_bytes': DEFAULT_MAX_COMMAND_BYTES,
        'max_marker_bytes': DEFAULT_MAX_MARKER_BYTES,
        'risk': 'terminal_command_templates',
        'payload_exposure': 'terminal_stream',
        'commands': COMMON_PYTHON3_COMMANDS,
    },
    'builtin:linux-sh-python3': {
        'schema_version': 1,
        'name': 'builtin:linux-sh-python3',
        'shell': 'sh',
        'directions': ['put', 'get'],
        'requires': ['python3'],
        'newline': 'submit_after',
        'path_encoding': 'b64url_utf8',
        'chunk_encoding': 'b64url',
        'max_command_bytes': DEFAULT_MAX_COMMAND_BYTES,
        'max_marker_bytes': DEFAULT_MAX_MARKER_BYTES,
        'risk': 'terminal_command_templates',
        'payload_exposure': 'terminal_stream',
        'commands': COMMON_PYTHON3_COMMANDS,
    },
    'builtin:macos-zsh-python3': {
        'schema_version': 1,
        'name': 'builtin:macos-zsh-python3',
        'shell': 'zsh',
        'directions': ['put', 'get'],
        'requires': ['python3'],
        'newline': 'submit_after',
        'path_encoding': 'b64url_utf8',
        'chunk_encoding': 'b64url',
        'max_command_bytes': DEFAULT_MAX_COMMAND_BYTES,
        'max_marker_bytes': DEFAULT_MAX_MARKER_BYTES,
        'risk': 'terminal_command_templates',
        'payload_exposure': 'terminal_stream',
        'commands': COMMON_PYTHON3_COMMANDS,
    },
    'builtin:freebsd-tcsh-python3.11': {
        'schema_version': 1,
        'name': 'builtin:freebsd-tcsh-python3.11',
        'shell': 'tcsh',
        'directions': ['put', 'get'],
        'requires': ['/usr/local/bin/python3.11'],
        'newline': 'submit_after',
        'path_encoding': 'b64url_utf8',
        'chunk_encoding': 'b64url',
        'max_command_bytes': DEFAULT_MAX_COMMAND_BYTES,
        'max_marker_bytes': DEFAULT_MAX_MARKER_BYTES,
        'risk': 'terminal_command_templates',
        'payload_exposure': 'terminal_stream',
        'commands': python_commands_for('/usr/local/bin/python3.11'),
    },
    'builtin:freebsd-tcsh-python-auto': {
        'schema_version': 1,
        'name': 'builtin:freebsd-tcsh-python-auto',
        'shell': 'tcsh',
        'directions': ['put', 'get'],
        'requires': ['python3 candidate'],
        'newline': 'submit_after',
        'path_encoding': 'b64url_utf8',
        'chunk_encoding': 'b64url',
        'max_command_bytes': DEFAULT_MAX_COMMAND_BYTES,
        'max_marker_bytes': DEFAULT_MAX_MARKER_BYTES,
        'risk': 'terminal_command_templates',
        'payload_exposure': 'terminal_stream',
        'auto_probe': {
            'kind': 'python_executable',
            'candidates': list(FREEBSD_TCSH_PYTHON_CANDIDATES),
        },
        'commands': COMMON_PYTHON3_COMMANDS,
    },
    'builtin:windows-powershell': {
        'schema_version': 1,
        'name': 'builtin:windows-powershell',
        'shell': 'powershell',
        'directions': ['put', 'get'],
        'requires': ['powershell'],
        'newline': 'submit_after',
        'path_encoding': 'b64url_utf8',
        'chunk_encoding': 'b64url',
        'max_command_bytes': DEFAULT_MAX_COMMAND_BYTES,
        'max_marker_bytes': DEFAULT_MAX_MARKER_BYTES,
        'risk': 'terminal_command_templates',
        'payload_exposure': 'terminal_stream',
        'commands': WINDOWS_POWERSHELL_COMMANDS,
    },
}

BUILTIN_ALIASES = {
    'builtin:py3-tcsh': 'builtin:freebsd-tcsh-python3',
    'builtin:py3-sh': 'builtin:linux-sh-python3',
    'py3-tcsh': 'builtin:freebsd-tcsh-python3',
    'py3-sh': 'builtin:linux-sh-python3',
    'freebsd-tcsh-python3': 'builtin:freebsd-tcsh-python3',
    'freebsd-tcsh-python3.11': 'builtin:freebsd-tcsh-python3.11',
    'freebsd-tcsh-python-auto': 'builtin:freebsd-tcsh-python-auto',
    'linux-sh-python3': 'builtin:linux-sh-python3',
    'macos-zsh-python3': 'builtin:macos-zsh-python3',
    'windows-powershell': 'builtin:windows-powershell',
}


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def b64url_decode(text):
    return base64.urlsafe_b64decode((text + '=' * ((4 - len(text) % 4) % 4)).encode('ascii'))


def b64url_json(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return b64url_encode(data)


def json_from_b64url(text):
    return json.loads(b64url_decode(text).decode('utf-8'))


def path_to_b64url(path):
    return b64url_encode(path.encode('utf-8'))


def new_nonce():
    return b64url_encode(os.urandom(NONCE_BYTES))


def validate_safe_value(name, value):
    text = str(value)
    if not SAFE_VALUE_RE.fullmatch(text):
        raise SystemExit(f'unsafe template value for {name}')
    return text


def render_command_template(template, values):
    literal_left = '\x00RSFILE_LBRACE\x00'
    literal_right = '\x00RSFILE_RBRACE\x00'
    working = template.replace('{{', literal_left).replace('}}', literal_right)
    names = set(PLACEHOLDER_RE.findall(working))
    unknown = sorted(names - set(values))
    if unknown:
        raise SystemExit('unknown command template placeholder: ' + ', '.join(unknown))
    rendered = working
    for name in sorted(names):
        rendered = rendered.replace('{' + name + '}', validate_safe_value(name, values[name]))
    if PLACEHOLDER_RE.search(rendered):
        raise SystemExit('unresolved command template placeholder')
    if '{' in rendered or '}' in rendered:
        raise SystemExit('literal braces in command templates must be doubled')
    return rendered.replace(literal_left, '{').replace(literal_right, '}')


def parse_frame_line(line, max_marker_bytes=DEFAULT_MAX_MARKER_BYTES):
    line = line.rstrip('\r\n')
    if len(line.encode('utf-8', errors='ignore')) > max_marker_bytes:
        return None
    match = FRAME_RE.fullmatch(line)
    if not match:
        return None
    nonce, seq_text, kind, payload_b64url = match.groups()
    try:
        payload = json_from_b64url(payload_b64url)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {
            'status': 'failed',
            'error_code': 'invalid_frame_payload',
            'nonce': nonce,
            'seq': int(seq_text),
            'kind': kind,
        }
    return {
        'status': 'ok',
        'nonce': nonce,
        'seq': int(seq_text),
        'kind': kind,
        'payload': payload,
        'raw': line,
    }


def capture_event_text(result):
    capture = result.get('capture') if isinstance(result, dict) else None
    events = capture.get('events') if isinstance(capture, dict) else None
    if not isinstance(events, list):
        return ''
    return ''.join(event.get('data', '') for event in events if isinstance(event, dict))


def extract_expected_frame(result, nonce, seq, expected_kind, max_marker_bytes=DEFAULT_MAX_MARKER_BYTES):
    capture = result.get('capture') if isinstance(result, dict) else None
    if not isinstance(capture, dict):
        raise SystemExit('send capture missing from response')
    if capture.get('status') == 'timeout' or capture.get('timed_out') is True:
        raise SystemExit('send capture timed out')
    if capture.get('gap', {}).get('detected'):
        raise SystemExit('send capture reported a tail gap')

    matches = []
    for line in capture_event_text(result).splitlines():
        if not line.startswith(FRAME_PREFIX + ':'):
            continue
        frame = parse_frame_line(line, max_marker_bytes=max_marker_bytes)
        if not frame:
            continue
        if frame.get('nonce') == nonce and frame.get('seq') == seq:
            matches.append(frame)

    if len(matches) != 1:
        raise SystemExit(f'expected one frame for seq {seq}, got {len(matches)}')
    frame = matches[0]
    if frame.get('status') != 'ok':
        raise SystemExit(frame.get('error_code') or 'invalid frame')
    if frame['kind'] == 'ERR':
        payload = frame.get('payload') if isinstance(frame.get('payload'), dict) else {}
        raise SystemExit(payload.get('error_code') or 'remote command failed')
    if frame['kind'] != expected_kind:
        raise SystemExit(f'expected frame kind {expected_kind}, got {frame["kind"]}')
    return frame


def redact_result(result):
    output = copy.deepcopy(result)
    if isinstance(output, dict):
        output.pop('token', None)
    return output


def load_method_pack(path, trust_pack):
    if not trust_pack:
        raise SystemExit('external method packs require --trust-pack')
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError as exc:
        raise SystemExit(f'failed to read method pack: {exc}') from exc
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f'failed to parse method pack JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise SystemExit('method pack JSON must be an object')
    methods = payload.get('methods')
    if isinstance(methods, list):
        result = {}
        for method in methods:
            validate_method(method)
            result[method['name']] = method
    else:
        validate_method(payload)
        result = {payload['name']: payload}
    pack_hash = hashlib.sha256(raw).hexdigest()
    return result, pack_hash


def validate_method(method):
    if not isinstance(method, dict):
        raise SystemExit('method must be an object')
    if method.get('schema_version') != 1:
        raise SystemExit('method schema_version must be 1')
    if not isinstance(method.get('name'), str) or not method['name']:
        raise SystemExit('method name is required')
    if method.get('path_encoding') != 'b64url_utf8':
        raise SystemExit('only path_encoding=b64url_utf8 is supported')
    if method.get('chunk_encoding') != 'b64url':
        raise SystemExit('only chunk_encoding=b64url is supported')
    commands = method.get('commands')
    if not isinstance(commands, dict):
        raise SystemExit('method commands must be an object')
    for name, command in commands.items():
        if not isinstance(command, dict) or not isinstance(command.get('template'), str):
            raise SystemExit(f'method command {name} must include a template')


def resolve_method(name, method_pack=None, trust_pack=False):
    name = BUILTIN_ALIASES.get(name, name)
    methods = dict(BUILTIN_METHODS)
    pack_hash = None
    if method_pack:
        packed_methods, pack_hash = load_method_pack(method_pack, trust_pack)
        methods.update(packed_methods)
    if name in BUILTIN_METHODS:
        return copy.deepcopy(BUILTIN_METHODS[name]), None
    if name in methods:
        return copy.deepcopy(methods[name]), pack_hash
    prefixed = BUILTIN_ALIASES.get('builtin:' + name, 'builtin:' + name)
    if not name.startswith('builtin:') and prefixed in BUILTIN_METHODS:
        return copy.deepcopy(BUILTIN_METHODS[prefixed]), None
    raise SystemExit(f'unknown rsfile method: {name}')


def command_spec(method, name):
    commands = method.get('commands', {})
    spec = commands.get(name)
    if not isinstance(spec, dict):
        raise SystemExit(f'method {method["name"]} does not support command {name}')
    return spec


def rendered_command(method, command_name, values):
    spec = command_spec(method, command_name)
    command = render_command_template(spec['template'], values)
    max_bytes = int(method.get('max_command_bytes') or DEFAULT_MAX_COMMAND_BYTES)
    byte_count = len(command.encode('utf-8'))
    if byte_count > max_bytes:
        raise SystemExit(f'{command_name} command is {byte_count} bytes, exceeds method cap {max_bytes}')
    return command


def base_values(nonce, seq, remote_path, **extra):
    values = {
        'nonce': nonce,
        'seq': str(seq),
        'rfile_b64url': path_to_b64url(remote_path),
    }
    values.update({key: str(value) for key, value in extra.items()})
    return values


class AgentTransport:
    def __init__(self, args):
        self.args = args

    def command(self, op, **fields):
        payload = {
            'op': op,
            'terminal_id': self.args.terminal,
        }
        if self.args.token:
            payload['token'] = self.args.token
        payload.update(fields)
        try:
            _status, result = agent_cli.post_json(
                self.args.url,
                payload,
                dev_mode=not bool(self.args.token),
                ca_file=self.args.ca_file,
                insecure=self.args.insecure,
            )
        except urllib.error.URLError as exc:
            raise SystemExit(f'external agent request failed: {exc}') from exc
        if not isinstance(result, dict):
            raise SystemExit('external agent response must be a JSON object')
        if result.get('status') == 'failed':
            raise SystemExit(result.get('error_code') or 'external agent command failed')
        return result

    def hello(self):
        return self.command('hello')

    def send_capture(self, text, wait_ms=DEFAULT_WAIT_MS, settle_ms=DEFAULT_SETTLE_MS):
        return self.command(
            'send',
            kind='text',
            text=text,
            capture=True,
            submit_after=True,
            wait_ms=wait_ms,
            settle_ms=settle_ms,
            limit=20,
        )


def preflight(transport):
    result = transport.hello()
    capabilities = result.get('capabilities')
    if not isinstance(capabilities, list):
        raise SystemExit('hello response did not include typed capabilities')
    missing = [capability for capability in ('send', 'send_capture', 'submit_after') if capability not in capabilities]
    if missing:
        raise SystemExit('external agent missing capabilities: ' + ', '.join(missing))
    return capabilities


def run_remote_command(transport, method, nonce, seq, command_name, remote_path, values, wait_ms):
    spec = command_spec(method, command_name)
    command = rendered_command(method, command_name, values)
    result = transport.send_capture(command, wait_ms=wait_ms)
    return extract_expected_frame(
        result,
        nonce=nonce,
        seq=seq,
        expected_kind=spec.get('expect', 'OK'),
        max_marker_bytes=int(method.get('max_marker_bytes') or DEFAULT_MAX_MARKER_BYTES),
    )


def auto_resolve_method(method, transport, wait_ms):
    auto_probe = method.get('auto_probe')
    if not isinstance(auto_probe, dict):
        return method, None
    if auto_probe.get('kind') != 'python_executable':
        raise SystemExit('unsupported auto_probe kind')
    failures = []
    nonce = new_nonce()
    for index, executable in enumerate(auto_probe.get('candidates') or [], start=1):
        candidate = copy.deepcopy(method)
        candidate['name'] = method['name'] + ':' + executable
        candidate['requires'] = [executable]
        candidate.pop('auto_probe', None)
        candidate['commands'] = python_commands_for(executable)
        try:
            frame = run_remote_command(
                transport,
                candidate,
                nonce,
                index,
                'probe',
                '.',
                {'nonce': nonce, 'seq': str(index)},
                min(wait_ms, 3000),
            )
        except SystemExit as exc:
            failures.append({'candidate': executable, 'error': str(exc)})
            try:
                transport.command('send', kind='text', text='\x03')
            except SystemExit:
                pass
            continue
        payload = frame.get('payload') if isinstance(frame, dict) else {}
        candidate['name'] = method['name'] + ':' + executable
        return candidate, {
            'candidate': executable,
            'payload': payload,
            'failures': failures,
        }
    raise SystemExit('no Python candidate succeeded for ' + method['name'])


def max_put_chunk_chars(method, remote_path, nonce, seq, overwrite_flag):
    values = base_values(
        nonce,
        seq,
        remote_path,
        offset=0,
        chunk_b64url='',
        overwrite_flag=overwrite_flag,
    )
    command = render_command_template(command_spec(method, 'write_chunk')['template'], values)
    cap = int(method.get('max_command_bytes') or DEFAULT_MAX_COMMAND_BYTES)
    available = cap - len(command.encode('utf-8')) - 32
    if available < 64:
        raise SystemExit('write_chunk template leaves too little room for payload')
    return max(64, available - available % 4)


def iter_text_chunks(text, chunk_chars):
    for index in range(0, len(text), chunk_chars):
        yield text[index:index + chunk_chars]


def local_file_metadata(path):
    with open(path, 'rb') as handle:
        data = handle.read()
    return {
        'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'data_b64url': b64url_encode(data),
    }


def do_probe(args, method, transport):
    capabilities = preflight(transport)
    method, auto_result = auto_resolve_method(method, transport, args.wait_ms)
    nonce = new_nonce()
    frame = run_remote_command(
        transport,
        method,
        nonce,
        1,
        'probe',
        args.remote_path or '.',
        {'nonce': nonce, 'seq': '1'},
        args.wait_ms,
    )
    return {
        'status': 'completed',
        'operation': 'probe',
        'transport': 'terminal_stream',
        'transcript_payload_exposed': True,
        'method': method['name'],
        'auto_probe': auto_result,
        'external_agent_capabilities_used': [cap for cap in ('send', 'send_capture', 'submit_after') if cap in capabilities],
        'method_capabilities': method.get('directions', []),
        'remote': frame['payload'],
    }


def do_put(args, method, transport):
    capabilities = preflight(transport)
    method, auto_result = auto_resolve_method(method, transport, args.wait_ms)
    meta = local_file_metadata(args.local)
    nonce = new_nonce()
    overwrite_flag = '1' if args.overwrite else '0'
    seq = 1

    run_remote_command(
        transport,
        method,
        nonce,
        seq,
        'probe',
        args.remote_path,
        {'nonce': nonce, 'seq': str(seq)},
        args.wait_ms,
    )
    seq += 1
    run_remote_command(
        transport,
        method,
        nonce,
        seq,
        'init_put',
        args.remote_path,
        base_values(nonce, seq, args.remote_path, overwrite_flag=overwrite_flag),
        args.wait_ms,
    )
    seq += 1

    chunk_chars = args.chunk_chars or max_put_chunk_chars(method, args.remote_path, nonce, seq, overwrite_flag)
    chunk_chars -= chunk_chars % 4
    if chunk_chars <= 0:
        raise SystemExit('invalid chunk size')
    if chunk_chars > max_put_chunk_chars(method, args.remote_path, nonce, seq, overwrite_flag):
        raise SystemExit('requested --chunk-chars exceeds rendered command byte cap')

    offset = 0
    chunk_count = 0
    start = time.monotonic()
    try:
        for chunk in iter_text_chunks(meta['data_b64url'], chunk_chars):
            frame = run_remote_command(
                transport,
                method,
                nonce,
                seq,
                'write_chunk',
                args.remote_path,
                base_values(nonce, seq, args.remote_path, offset=offset, chunk_b64url=chunk),
                args.wait_ms,
            )
            payload = frame.get('payload', {})
            if payload.get('cmd') != 'write_chunk' or int(payload.get('offset', -1)) != offset:
                raise SystemExit('write_chunk response did not match expected offset')
            if int(payload.get('length', -1)) != len(chunk):
                raise SystemExit('write_chunk response did not match expected length')
            offset += len(chunk)
            chunk_count += 1
            seq += 1

        frame = run_remote_command(
            transport,
            method,
            nonce,
            seq,
            'finish_put',
            args.remote_path,
            base_values(
                nonce,
                seq,
                args.remote_path,
                expected_size=meta['bytes'],
                expected_sha256=meta['sha256'],
                overwrite_flag=overwrite_flag,
            ),
            args.wait_ms,
        )
    finally:
        if args.cleanup:
            try:
                run_remote_command(
                    transport,
                    method,
                    nonce,
                    seq + 1,
                    'cleanup',
                    args.remote_path,
                    base_values(nonce, seq + 1, args.remote_path),
                    min(args.wait_ms, 3000),
                )
            except SystemExit:
                pass

    payload = frame.get('payload', {})
    if payload.get('sha256') != meta['sha256'] or int(payload.get('bytes', -1)) != meta['bytes']:
        raise SystemExit('finish_put response did not match local metadata')
    return {
        'status': 'completed',
        'operation': 'put',
        'transport': 'terminal_stream',
        'transcript_payload_exposed': True,
        'remote_echo_expected': True,
        'method': method['name'],
        'auto_probe': auto_result,
        'external_agent_capabilities_used': [cap for cap in ('send', 'send_capture', 'submit_after') if cap in capabilities],
        'local': args.local,
        'remote_path': args.remote_path,
        'bytes': meta['bytes'],
        'sha256': meta['sha256'],
        'chunk_count': chunk_count,
        'chunk_chars': chunk_chars,
        'elapsed_seconds': round(time.monotonic() - start, 3),
    }


def do_get(args, method, transport):
    if not args.allow_get:
        raise SystemExit('get requires --allow-get because remote bytes are exposed in terminal output')
    capabilities = preflight(transport)
    method, auto_result = auto_resolve_method(method, transport, args.wait_ms)
    nonce = new_nonce()
    seq = 1

    run_remote_command(
        transport,
        method,
        nonce,
        seq,
        'probe',
        args.remote_path,
        {'nonce': nonce, 'seq': str(seq)},
        args.wait_ms,
    )
    seq += 1
    stat_frame = run_remote_command(
        transport,
        method,
        nonce,
        seq,
        'stat_hash',
        args.remote_path,
        base_values(nonce, seq, args.remote_path),
        args.wait_ms,
    )
    stat_payload = stat_frame.get('payload', {})
    expected_size = int(stat_payload.get('bytes', -1))
    expected_sha = stat_payload.get('sha256')
    if expected_size < 0 or not isinstance(expected_sha, str):
        raise SystemExit('stat_hash response missing bytes or sha256')
    if expected_size > args.max_bytes:
        raise SystemExit(f'remote file is {expected_size} bytes, exceeds --max-bytes {args.max_bytes}')
    seq += 1

    chunk_bytes = args.chunk_bytes or DEFAULT_GET_CHUNK_BYTES
    if chunk_bytes <= 0:
        raise SystemExit('--chunk-bytes must be positive')
    part_path = args.local + '.part'
    digest = hashlib.sha256()
    offset = 0
    chunk_count = 0
    start = time.monotonic()
    with open(part_path, 'wb') as handle:
        while offset < expected_size:
            length = min(chunk_bytes, expected_size - offset)
            frame = run_remote_command(
                transport,
                method,
                nonce,
                seq,
                'read_chunk',
                args.remote_path,
                base_values(nonce, seq, args.remote_path, offset=offset, length=length),
                args.wait_ms,
            )
            payload = frame.get('payload', {})
            if payload.get('cmd') != 'read_chunk' or int(payload.get('offset', -1)) != offset:
                raise SystemExit('read_chunk response did not match expected offset')
            data = b64url_decode(payload.get('data_b64url', ''))
            if len(data) != int(payload.get('length', -1)) or len(data) != length:
                raise SystemExit('read_chunk response length mismatch')
            handle.write(data)
            digest.update(data)
            offset += len(data)
            chunk_count += 1
            seq += 1

    got_sha = digest.hexdigest()
    if got_sha != expected_sha:
        raise SystemExit('downloaded file sha256 mismatch')
    os.replace(part_path, args.local)
    return {
        'status': 'completed',
        'operation': 'get',
        'transport': 'terminal_stream',
        'transcript_payload_exposed': True,
        'remote_echo_expected': True,
        'method': method['name'],
        'auto_probe': auto_result,
        'external_agent_capabilities_used': [cap for cap in ('send', 'send_capture', 'submit_after') if cap in capabilities],
        'local': args.local,
        'remote_path': args.remote_path,
        'bytes': expected_size,
        'sha256': expected_sha,
        'chunk_count': chunk_count,
        'chunk_bytes': chunk_bytes,
        'elapsed_seconds': round(time.monotonic() - start, 3),
    }


def add_common_connection_args(parser):
    parser.add_argument('--handoff', help='Read url, token, and terminal from a StandTerm external agent handoff JSON file')
    parser.add_argument('--agentinfo', help='Read tokenless StandTerm agentinfo JSON from a local path or URL')
    parser.add_argument('--url', help='StandTerm base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')


def add_method_args(parser):
    parser.add_argument('--method', default='builtin:freebsd-tcsh-python3', help='Remote command recipe method')
    parser.add_argument('--method-pack', help='Trusted JSON method pack path')
    parser.add_argument('--trust-pack', action='store_true', help='Allow executing commands from --method-pack')


def parse_args():
    parser = argparse.ArgumentParser(
        description='StandTerm terminal-stream fallback file transfer helper',
    )
    add_common_connection_args(parser)
    add_method_args(parser)
    parser.add_argument('--wait-ms', type=int, default=DEFAULT_WAIT_MS, help='Maximum wait for each remote command marker')
    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('list-methods')

    print_parser = subparsers.add_parser('print-method')

    probe_parser = subparsers.add_parser('probe')
    probe_parser.add_argument('--remote-path', default='.', help='Remote path only used to exercise path encoding')

    put_parser = subparsers.add_parser('put')
    put_parser.add_argument('--local', required=True, help='Local file to upload')
    put_parser.add_argument('--remote-path', required=True, help='Remote destination path')
    put_parser.add_argument('--overwrite', action='store_true', help='Replace an existing remote destination')
    put_parser.add_argument('--chunk-chars', type=int, help='Override base64url payload chars per write_chunk command')
    put_parser.add_argument('--no-cleanup', dest='cleanup', action='store_false', help='Leave temporary remote files after success')
    put_parser.set_defaults(cleanup=True)

    get_parser = subparsers.add_parser('get')
    get_parser.add_argument('--remote-path', required=True, help='Remote source path')
    get_parser.add_argument('--local', required=True, help='Local destination path')
    get_parser.add_argument('--allow-get', action='store_true', help='Acknowledge that remote bytes will pass through terminal output')
    get_parser.add_argument('--max-bytes', type=int, required=True, help='Maximum remote file size to download')
    get_parser.add_argument('--chunk-bytes', type=int, default=DEFAULT_GET_CHUNK_BYTES, help='Raw bytes per read_chunk command')

    args = parser.parse_args()
    agent_cli.apply_agentinfo(args)
    if args.command not in {'list-methods', 'print-method'}:
        agent_cli.apply_handoff(args)
        if not args.url:
            parser.error('--url is required unless --handoff or --agentinfo provides url')
    elif args.handoff:
        agent_cli.apply_handoff(args)
    return args


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main():
    args = parse_args()
    method, pack_hash = resolve_method(args.method, method_pack=args.method_pack, trust_pack=args.trust_pack)
    validate_method(method)
    if args.command == 'list-methods':
        names = sorted(BUILTIN_METHODS)
        print_json({'status': 'completed', 'builtin_methods': names})
        return 0
    if args.command == 'print-method':
        output = copy.deepcopy(method)
        output['method_pack_sha256'] = pack_hash
        print_json(output)
        return 0

    transport = AgentTransport(args)
    if args.command == 'probe':
        result = do_probe(args, method, transport)
    elif args.command == 'put':
        result = do_put(args, method, transport)
    elif args.command == 'get':
        result = do_get(args, method, transport)
    else:
        raise SystemExit(f'unsupported command: {args.command}')
    print_json(redact_result(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
