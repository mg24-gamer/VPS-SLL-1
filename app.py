import os
import signal
import subprocess
import threading
import time
import shutil
import zipfile
import psutil
import json
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file

app = Flask(__name__)

# --- CONFIGURATION ---
BASE_DIR = os.getcwd()
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'user_files')
DB_FILE = 'servers_db.json'

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

SERVERS = {}

# --- PERSISTENCE ---
def save_servers():
    data = {
        sid: {
            'cmd': s['cmd'], 
            'cwd': s.get('cwd', ''), 
            'path': s['path'], 
            'auto_restart': s.get('auto_restart', False),
            'restart_interval': s.get('restart_interval', '1h'),
            'status': 'stopped'
        } for sid, s in SERVERS.items()
    }
    with open(DB_FILE, 'w') as f:
        json.dump(data, f)

def load_servers():
    global SERVERS
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                saved = json.load(f)
                for sid, s in saved.items():
                    SERVERS[sid] = {
                        'process': None,
                        'cmd': s['cmd'],
                        'cwd': s.get('cwd', ''),
                        'auto_restart': s.get('auto_restart', False),
                        'restart_interval': s.get('restart_interval', '1h'),
                        'logs': ["Restored..."],
                        'status': 'stopped',
                        'path': s['path'],
                        'last_start_time': 0
                    }
        except: pass

load_servers()

# --- HELPER FUNCTIONS ---
def get_system_stats():
    try:
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
    except:
        cpu, ram = 0, 0
    return cpu, ram

def log_monitor(server_id, proc_obj):
    server = SERVERS.get(server_id)
    if not server: return

    for line in iter(proc_obj.stdout.readline, ''):
        # Stop logging if the server gets deleted OR if a new process has replaced this one
        if server_id not in SERVERS or SERVERS[server_id].get('process') != proc_obj:
            break
        if line:
            SERVERS[server_id]['logs'].append(line.strip())
            if len(SERVERS[server_id]['logs']) > 500:
                SERVERS[server_id]['logs'] = SERVERS[server_id]['logs'][-500:]

    proc_obj.stdout.close()
    
    # Only mark as stopped if THIS process is still the active process
    if server_id in SERVERS and SERVERS[server_id].get('process') == proc_obj:
        SERVERS[server_id]['status'] = 'stopped'
        SERVERS[server_id]['logs'].append(">>> Process Exited.")

def kill_process_completely(proc):
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.terminate()
        parent.terminate()
        gone, alive = psutil.wait_procs(parent.children(), timeout=3)
        for p in alive:
            p.kill()
        parent.kill()
    except: pass

def run_install_command(server_id, command):
    if server_id in SERVERS:
        SERVERS[server_id]['logs'].append(f">>> Installing: {command}")
        try:
            process = subprocess.Popen(
                command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in iter(process.stdout.readline, ''):
                if line:
                    SERVERS[server_id]['logs'].append(line.strip())
            SERVERS[server_id]['logs'].append(">>> Installation Finished.")
        except Exception as e:
            SERVERS[server_id]['logs'].append(f"Install Error: {str(e)}")

def start_server_internal(server_id, server):
    if server['status'] == 'running':
        return

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    work_dir = os.path.join(server['path'], server.get('cwd', ''))
    if not os.path.exists(work_dir):
        work_dir = server['path']

    proc = subprocess.Popen(
        server['cmd'],
        shell=True,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        env=env,
        preexec_fn=os.setsid if os.name != 'nt' else None
    )
    server['process'] = proc
    server['status'] = 'running'
    server['last_start_time'] = time.time()
    
    # Pass the specific process object to the thread to prevent race conditions
    threading.Thread(target=log_monitor, args=(server_id, proc), daemon=True).start()

def auto_restarter():
    while True:
        time.sleep(5) # Fast interval for precise 30s checks
        current_time = time.time()
        for server_id, server in list(SERVERS.items()):
            if server.get('status') == 'running' and server.get('auto_restart'):
                interval_str = server.get('restart_interval', '1h')
                interval_map = {
                    '30s': 30, '1m': 60, '5m': 300, '10m': 600, '30m': 1800, 
                    '1h': 3600, '2h': 7200, '3h': 10800, 
                    '6h': 21600, '12h': 43200, '24h': 86400
                }
                interval_sec = interval_map.get(interval_str, 3600)
                last_start = server.get('last_start_time', current_time)
                
                if current_time - last_start >= interval_sec:
                    server['logs'].append(f">>> Auto-restarting server (Interval: {interval_str})...")
                    if server.get('process'):
                        kill_process_completely(server['process'])
                        server['process'] = None
                    server['status'] = 'stopped'
                    start_server_internal(server_id, server)

# Start auto-restarter thread
threading.Thread(target=auto_restarter, daemon=True).start()

# --- ROUTES ---
@app.route('/')
def index():
    cpu, ram = get_system_stats()
    return render_template('index.html', servers=SERVERS, cpu=cpu, ram=ram,
                           total_count=len(SERVERS),
                           running_count=sum(1 for s in SERVERS.values() if s['status'] == 'running'))

@app.route('/create_server', methods=['POST'])
def create_server():
    server_name = request.form.get('server_name').strip().replace(" ", "_")
    start_command = request.form.get('start_command').strip()

    if not server_name or server_name in SERVERS:
        return "Exists", 400

    file = request.files.get('file')
    server_path = os.path.join(UPLOAD_FOLDER, server_name)
    os.makedirs(server_path, exist_ok=True)

    if file:
        file_path = os.path.join(server_path, file.filename)
        file.save(file_path)
        if file.filename.endswith('.zip'):
            try:
                with zipfile.ZipFile(file_path, 'r') as z:
                    z.extractall(server_path)
            except: pass

    SERVERS[server_name] = {
        'process': None, 'cmd': start_command, 'cwd': '', 'logs': ["Server Created."],
        'auto_restart': False, 'restart_interval': '1h', 'last_start_time': 0,
        'status': 'stopped', 'path': server_path
    }
    save_servers()
    return redirect(url_for('index'))

@app.route('/action/<server_id>/<action>')
def server_action(server_id, action):
    if server_id not in SERVERS:
        return jsonify({'error': 'Not found'})
    server = SERVERS[server_id]

    try:
        if action == 'start':
            start_server_internal(server_id, server)

        elif action == 'stop':
            if server['process']:
                kill_process_completely(server['process'])
                server['process'] = None
            server['status'] = 'stopped'
            server['logs'].append(">>> Stopped by User")
            
        elif action == 'restart':
            if server['process']:
                kill_process_completely(server['process'])
                server['process'] = None
            server['status'] = 'stopped'
            server['logs'].append(">>> Manual Restart Triggered...")
            start_server_internal(server_id, server)

        elif action == 'delete':
            if server['process']:
                kill_process_completely(server['process'])
            shutil.rmtree(server['path'], ignore_errors=True)
            del SERVERS[server_id]
            save_servers()
            return redirect(url_for('index'))

    except Exception as e:
        server['logs'].append(f"Error: {e}")

    return redirect(url_for('index'))

# --- FILE MANAGEMENT ---
@app.route('/get_logs/<server_id>')
def get_logs(server_id):
    if server_id in SERVERS:
        return jsonify({'logs': "\n".join(SERVERS[server_id]['logs'])})
    return jsonify({'logs': ''})

@app.route('/send_input/<server_id>', methods=['POST'])
def send_input(server_id):
    cmd = request.form.get('command')
    if server_id in SERVERS and SERVERS[server_id]['process']:
        proc = SERVERS[server_id]['process']
        try:
            if proc.stdin:
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
                return jsonify({'status': 'ok'})
        except: pass
    return jsonify({'status': 'error'})

@app.route('/files/<server_id>')
def list_files(server_id):
    if server_id in SERVERS:
        subpath = request.args.get('path', '')
        if '..' in subpath: subpath = ''

        full_path = os.path.join(SERVERS[server_id]['path'], subpath)
        if not os.path.exists(full_path): full_path = SERVERS[server_id]['path']

        files = []
        try:
            for f in os.listdir(full_path):
                fp = os.path.join(full_path, f)
                size = os.path.getsize(fp) if os.path.isfile(fp) else 0
                files.append({'name': f, 'size': f"{size/1024:.1f} KB", 'type': 'file' if os.path.isfile(fp) else 'dir'})
        except: pass

        return jsonify({
            'files': files,
            'cmd': SERVERS[server_id]['cmd'],
            'cwd': SERVERS[server_id].get('cwd', ''),
            'auto_restart': SERVERS[server_id].get('auto_restart', False),
            'restart_interval': SERVERS[server_id].get('restart_interval', '1h'),
            'current_path': subpath
        })
    return jsonify({'files': []})

@app.route('/upload/<server_id>', methods=['POST'])
def upload_file(server_id):
    if server_id in SERVERS:
        file = request.files.get('file')
        subpath = request.form.get('path', '')
        if '..' in subpath: subpath = ''
        target_dir = os.path.join(SERVERS[server_id]['path'], subpath)
        if file:
            file_path = os.path.join(target_dir, file.filename)
            file.save(file_path)
            if file.filename.endswith('.zip'):
                try:
                    with zipfile.ZipFile(file_path, 'r') as z:
                        z.extractall(target_dir)
                    os.remove(file_path)
                except: pass
    return jsonify({'status': 'ok'})

@app.route('/create_folder/<server_id>', methods=['POST'])
def create_folder(server_id):
    folder_name = request.form.get('name')
    subpath = request.form.get('path', '')
    if server_id in SERVERS and folder_name:
        target = os.path.join(SERVERS[server_id]['path'], subpath, folder_name)
        os.makedirs(target, exist_ok=True)
    return jsonify({'status': 'ok'})

@app.route('/download/<server_id>/<filename>')
def download_file(server_id, filename):
    if server_id in SERVERS:
        subpath = request.args.get('path', '')
        path = os.path.join(SERVERS[server_id]['path'], subpath, filename)
        if os.path.exists(path):
            return send_file(path, as_attachment=True)
    return "File not found", 404

@app.route('/delete_file/<server_id>/<filename>')
def delete_file(server_id, filename):
    if server_id in SERVERS:
        subpath = request.args.get('path', '')
        path = os.path.join(SERVERS[server_id]['path'], subpath, filename)
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    return jsonify({'status': 'ok'})

@app.route('/update_settings/<server_id>', methods=['POST'])
def update_settings(server_id):
    cmd = request.form.get('cmd')
    cwd = request.form.get('cwd')
    auto_restart = request.form.get('auto_restart') == 'true'
    restart_interval = request.form.get('restart_interval')
    
    if server_id in SERVERS:
        SERVERS[server_id]['cmd'] = cmd
        SERVERS[server_id]['cwd'] = cwd
        SERVERS[server_id]['auto_restart'] = auto_restart
        SERVERS[server_id]['restart_interval'] = restart_interval
        save_servers()
    return jsonify({'status': 'ok'})

@app.route('/install_pkg/<server_id>', methods=['POST'])
def install_pkg(server_id):
    pkg_type = request.form.get('type')
    pkg_name = request.form.get('name')
    if server_id in SERVERS and pkg_name:
        cmd = ""
        if pkg_type == 'pip':
            cmd = f"pip install {pkg_name}"
        elif pkg_type == 'pkg':
            cmd = f"pkg install -y {pkg_name}"
        if cmd:
            threading.Thread(target=run_install_command, args=(server_id, cmd)).start()
    return jsonify({'status': 'ok'})

@app.route("/ping")
def ping():
    return "alive"

@app.route("/json")
def json_alive():
    return jsonify({"status": "alive"})

# --- RUN SERVER ---
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, threaded=True)
