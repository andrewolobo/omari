## Linux

source myenv/bin/activate
omar_bot\.venv\Scripts\omari

## Windows

.venv\Scripts\activate
omar_bot\.venv\Scripts\omari

sudo -u omari bash -c '
cd /var/py/apps/omar_bot &&
rm -rf .venv &&
python3 -m venv --system-site-packages .venv &&
.venv/bin/pip install -r requirements.txt
'
