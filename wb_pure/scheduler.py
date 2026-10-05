"""Current-user timers only; no cloud scheduler."""
import getpass
import hashlib
import json
import os
import plistlib
import shlex
import subprocess
import sys
from datetime import datetime
from xml.sax.saxutils import escape


def configure(home, remove=False):
    ident = 'wb-pure-' + hashlib.sha256(str(home).encode()).hexdigest()[:12]
    command = [sys.executable, '-m', 'wb_pure', '--home', str(home), 'worker', '--once']
    if os.name == 'nt':
        if remove:
            subprocess.run(['schtasks', '/Delete', '/TN', ident, '/F'], check=True, capture_output=True)
        else:
            user = subprocess.check_output(['whoami'], text=True).strip()
            args = escape(subprocess.list2cmdline(command[1:]))
            xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{datetime.now().isoformat(timespec='seconds')}</StartBoundary><Enabled>true</Enabled></TimeTrigger><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="User"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT15M</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
<Actions Context="User"><Exec><Command>{escape(command[0])}</Command><Arguments>{args}</Arguments></Exec></Actions></Task>'''
            path = home / 'task.xml'
            path.write_text(xml, encoding='utf-16')
            subprocess.run(['schtasks', '/Create', '/TN', ident, '/XML', str(path), '/F'], check=True, capture_output=True)
    elif sys.platform == 'darwin':
        from pathlib import Path
        path = Path.home() / 'Library' / 'LaunchAgents' / (ident + '.plist')
        path.parent.mkdir(parents=True, exist_ok=True)
        domain = f'gui/{os.getuid()}'
        subprocess.run(['launchctl', 'bootout', domain, str(path)], capture_output=True)
        if remove:
            path.unlink(missing_ok=True)
        else:
            with path.open('wb') as f:
                plistlib.dump({'Label': ident, 'ProgramArguments': command, 'RunAtLoad': True,
                               'StartInterval': 60, 'ProcessType': 'Background'}, f)
            subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
    else:
        old = subprocess.run(['crontab', '-l'], text=True, capture_output=True)
        if old.returncode not in (0, 1):
            raise ValueError('cannot_read_crontab')
        lines = [line for line in old.stdout.splitlines() if not line.endswith('# ' + ident)]
        if not remove:
            lines.append('* * * * * ' + shlex.join(command).replace('%', r'\%') + ' >/dev/null 2>&1 # ' + ident)
        subprocess.run(['crontab', '-'], input='\n'.join(lines) + '\n', text=True, check=True)
    path = home / 'schedule.json'
    if remove:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps({'name': ident, 'installed_at': datetime.now().isoformat()}), encoding='utf-8')
    return {'schedule_installed': not remove, 'name': ident,
            'requirement': '电脑开机、联网，当前系统账号登录；恢复后自动补跑。'}
