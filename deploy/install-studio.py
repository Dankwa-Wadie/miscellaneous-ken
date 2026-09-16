#!/usr/bin/env python3
"""Install the local app and replace the old calendar scheduler with one service."""
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
LABEL='com.miscellaneousken.studio'
app=ROOT/'build/Miscellaneous Ken.app'
if not app.exists():
    raise SystemExit('Build the app first: bash deploy/build-app.sh')
if not (ROOT/'.venv/bin/python3').exists():
    raise SystemExit('Project virtual environment is missing')
folder=Path.home()/'Library/LaunchAgents'
folder.mkdir(parents=True,exist_ok=True)
plist=folder/(LABEL+'.plist')
logs=ROOT/'logs'
logs.mkdir(exist_ok=True)
# Preserve the old schedule file for rollback; unload its registration to avoid double runs.
for label in ('com.miscellaneousken.agent','com.miscellaneousken.runner',LABEL):
    subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],capture_output=True)
old=folder/'com.miscellaneousken.agent.plist'
if old.exists():
    old.rename(old.with_suffix('.plist.disabled'))
old_runner=folder/'com.miscellaneousken.runner.plist'
if old_runner.exists():
    old_runner.rename(old_runner.with_suffix('.plist.disabled'))
with plist.open('wb') as f:
    plistlib.dump(dict(Label=LABEL,ProgramArguments=['/bin/bash',str(ROOT/'deploy/run-studio.sh')],
        WorkingDirectory=str(ROOT),RunAtLoad=True,KeepAlive=True,ThrottleInterval=15,
        ProcessType='Background',StandardOutPath=str(logs/'studio.log'),StandardErrorPath=str(logs/'studio.err.log')),f)
settings=ROOT/'studio-settings.json'
if not settings.exists():
    # Preserve the existing automatic-upload behavior and current YouTube privacy.
    settings.write_text(json.dumps(dict(enabled=True,interval_hours=5,mode='upload',next_run=0),indent=2))
apps=Path.home()/'Applications'
apps.mkdir(exist_ok=True)
destination=apps/app.name
if destination.exists():
    shutil.rmtree(destination)
shutil.copytree(app,destination)
subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(plist)],check=True)
print(f'Installed {destination}')
print('Background service enabled; automation settings are in the app.')
