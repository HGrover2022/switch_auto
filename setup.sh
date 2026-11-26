#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv --system-site-packages .venv
fi

.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --only-binary=:all: pycryptodome
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
.venv/bin/python -B -c "import requests, yaml, bs4; from Crypto.Cipher import PKCS1_OAEP; print('deps ok')"
