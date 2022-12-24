#!/usr/bin/python
# -*- coding: utf-*-

import argparse
import os
import pathlib
import sys
import traceback
from filelock import FileLock
from autoreplier import AutoReplier, AutoReplierSettings, create_rotating_log

parser = argparse.ArgumentParser(prog='Autoreplier', description='Tool used to reply to incoming messages')
parser.add_argument('-l', help='Log level', default='INFO')
parser.add_argument('-f', required=True, help='Configuration file')
args = parser.parse_args()

LOG_LEVEL: str = args.l
if LOG_LEVEL.startswith('"') and LOG_LEVEL.endswith('"'):
    LOG_LEVEL = LOG_LEVEL[1:-1]
if LOG_LEVEL.startswith("'") and LOG_LEVEL.endswith("'"):
    LOG_LEVEL = LOG_LEVEL[1:-1]
CONFIG_PATH: str = args.f
if CONFIG_PATH.startswith('"') and CONFIG_PATH.endswith('"'):
    CONFIG_PATH = CONFIG_PATH[1:-1]
if CONFIG_PATH.startswith("'") and CONFIG_PATH.endswith("'"):
    CONFIG_PATH = CONFIG_PATH[1:-1]
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = str(pathlib.Path(__file__).parent) + os.sep + CONFIG_PATH
LOG_PATH: str = os.path.splitext(CONFIG_PATH)[0] + '.log'
LOCK_PATH: str = os.path.splitext(CONFIG_PATH)[0] + '.lck'
settings = AutoReplierSettings()
settings.parse(CONFIG_PATH)


if __name__ == '__main__':
    with FileLock(LOCK_PATH):
        try:
            AutoReplier(settings, create_rotating_log(LOG_PATH, LOG_LEVEL)).start()
        except KeyboardInterrupt:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_tb(exc_traceback, limit=6, file=sys.stderr)
            exit()
