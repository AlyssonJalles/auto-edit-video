#!/usr/bin/env python
"""
🎬 Auto Video Editor - Interface Web Moderna
Uma ferramenta poderosa para edição automática de vídeos com IA

🔒 AUTENTICAÇÃO:
    Configure ACCESS_PASSWORD no .env para proteger com senha.
    Sem senha configurada = acesso livre (modo local).
"""

import os
import sys
import glob
import shutil
import threading
import json
import time
import secrets
import re
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, jsonify, abort, redirect, url_for, session, Response
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

# Database local (SQLite)
import database as db

# Carrega .env se existir
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==================== CONFIGURAÇÕES DE SEGURANÇA ====================

# Gera SECRET_KEY aleatória se não existir no ambiente
# Para persistir entre reinícios, defina FLASK_SECRET_KEY no .env
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)

# Extensões de vídeo permitidas
ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}

# Tamanho máximo de nome de arquivo (segurança)
MAX_FILENAME_LENGTH = 255

# ==================== ESTRUTURA DE PASTAS ====================
# Pasta raiz do projeto
PROJECT_ROOT = Path(__file__).parent.resolve()

# Pasta de upload (onde os vídeos são armazenados)
UPLOAD_DIR = PROJECT_ROOT / 'upload'

# Pasta de vídeos processados
PROCESSED_DIR = UPLOAD_DIR / 'processados'

# Garante que as pastas existam
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(exist_ok=True)

# Workspace é a pasta de upload (para compatibilidade)
WORKSPACE_DIR = UPLOAD_DIR

def get_output_path(input_path: str, suffix: str) -> str:
    """
    Gera o caminho de saída na pasta processados.
    
    Args:
        input_path: Caminho do vídeo de entrada
        suffix: Sufixo a adicionar (ex: '_cut', '_legendado', '_final')
    
    Returns:
        Caminho completo na pasta processados
    """
    # Garante que a pasta processados existe
    PROCESSED_DIR.mkdir(exist_ok=True)
    
    # Pega apenas o nome do arquivo
    filename = os.path.basename(input_path)
    base, ext = os.path.splitext(filename)
    
    # Gera o novo nome
    output_filename = f"{base}{suffix}{ext}"
    
    return str(PROCESSED_DIR / output_filename)


def ensure_video_and_sidecars_in_processados(video_path: str):
    """
    Garante que uma cópia do vídeo (e arquivos .json, .ass etc.) está na pasta processados.
    Se o vídeo já estiver em processados, retorna o mesmo caminho. Caso contrário, copia
    o vídeo e os sidecars para processados e atualiza app_state["selected_video"].
    Retorna o caminho a ser usado (em processados) ou None em caso de erro.
    """
    if not video_path:
        return None
    is_valid, resolved_str = validate_video_path(video_path)
    if not is_valid:
        return None
    resolved = Path(resolved_str).resolve()
    processed_dir = PROCESSED_DIR.resolve()
    try:
        if processed_dir in resolved.parents or resolved.parent == processed_dir:
            return str(resolved)
        dest_video = processed_dir / resolved.name
        shutil.copy2(resolved, dest_video)
        base = resolved.stem
        for ext in (".json", ".ass"):
            sidecar = resolved.parent / (base + ext)
            if sidecar.is_file():
                shutil.copy2(sidecar, processed_dir / (base + ext))
        app_state["selected_video"] = str(dest_video)
        return str(dest_video)
    except Exception:
        return None


# ==================== AUTENTICAÇÃO ====================
# Se ACCESS_PASSWORD estiver definido, requer login
# Se ACCESS_PASSWORD_HASH estiver definido, usa o hash diretamente
# Se nenhum estiver definido, acesso é livre (modo local)

ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '')
ACCESS_PASSWORD_HASH = os.environ.get('ACCESS_PASSWORD_HASH', '')

# Se a senha em texto foi fornecida, gera o hash
if ACCESS_PASSWORD and not ACCESS_PASSWORD_HASH:
    ACCESS_PASSWORD_HASH = generate_password_hash(ACCESS_PASSWORD)
    # Aviso: em produção, use apenas o hash
    print("⚠️  AVISO: ACCESS_PASSWORD definido. Para maior segurança, use ACCESS_PASSWORD_HASH.")

# Verifica se autenticação está habilitada
AUTH_ENABLED = bool(ACCESS_PASSWORD_HASH)

# Tempo de sessão (em horas) - padrão 24h
SESSION_LIFETIME_HOURS = int(os.environ.get('SESSION_LIFETIME_HOURS', 24))

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB max upload
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=SESSION_LIFETIME_HOURS)

# CORS: Permite todas as origens para simplicidade em uso local/rede
# A proteção é feita via autenticação por senha quando habilitada
socketio = SocketIO(app, cors_allowed_origins="*")


# ==================== DECORADOR DE LOGIN ====================

def login_required(f):
    """
    Decorador que exige autenticação.
    Se AUTH_ENABLED=False, permite acesso livre.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not AUTH_ENABLED:
            return f(*args, **kwargs)
        
        if not session.get('authenticated'):
            # Para requisições AJAX, retorna 401
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Não autenticado', 'redirect': '/login'}), 401
            # Para requisições normais, redireciona
            return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    return decorated_function


def api_login_required(f):
    """
    Decorador para rotas de API que exige autenticação.
    Sempre retorna JSON.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not AUTH_ENABLED:
            return f(*args, **kwargs)
        
        if not session.get('authenticated'):
            return jsonify({'success': False, 'error': 'Não autenticado', 'redirect': '/login'}), 401
        
        return f(*args, **kwargs)
    return decorated_function

# Inicializa o banco de dados
db.init_db()

# Carrega configurações do banco
saved_settings = db.get_all_settings()

# Estado global da aplicação (volátil - não persiste)
app_state = {
    "selected_video": None,
    "is_processing": False,
    "cancel_requested": False,
    "api_key": os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or saved_settings.get("gemini_api_key", ""),
    "progress": 0,
    "progress_text": "Aguardando início..."
}

# Configurações persistentes (carrega do banco)
default_settings = {
    "whisper_model": saved_settings.get("whisper_model", "small"),
    "cut_method": saved_settings.get("cut_method", "speech"),
    "language": saved_settings.get("language", "pt"),
    "use_ai_correction": saved_settings.get("use_ai_correction", "true") == "true"
}

# Atualiza WORKSPACE_DIR do banco se não definido no ambiente
if not os.environ.get('WORKSPACE_DIR') and saved_settings.get('workspace_dir', '.') != '.':
    WORKSPACE_DIR = Path(saved_settings.get('workspace_dir')).resolve()


# ==================== FUNÇÕES DE SEGURANÇA ====================

def sanitize_filename(filename: str) -> str:
    """
    Sanitiza nome de arquivo removendo caracteres perigosos.
    Previne path traversal e injeção de comandos.
    """
    if not filename:
        return ""
    
    # Remove caracteres perigosos
    filename = os.path.basename(filename)  # Remove diretórios
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename)  # Remove chars ilegais
    filename = filename.strip('. ')  # Remove pontos/espaços iniciais e finais
    
    # Limita tamanho
    if len(filename) > MAX_FILENAME_LENGTH:
        name, ext = os.path.splitext(filename)
        filename = name[:MAX_FILENAME_LENGTH - len(ext)] + ext
    
    return filename


def is_safe_path(path: str, base_dir: Path = WORKSPACE_DIR) -> bool:
    """
    Verifica se o caminho é seguro e está dentro do diretório permitido.
    Previne ataques de path traversal (../).
    """
    try:
        # Resolve o caminho completo
        resolved_path = Path(path).resolve()
        
        # Verifica se está dentro do diretório base
        resolved_path.relative_to(base_dir)
        return True
    except (ValueError, RuntimeError):
        return False


def validate_video_path(video_path: str) -> tuple[bool, str]:
    """
    Valida se o caminho do vídeo é seguro e o arquivo existe.
    Aceita tanto caminho completo quanto apenas nome do arquivo.
    Retorna (is_valid, full_path ou error_message).
    """
    if not video_path:
        return False, "Caminho do vídeo não informado"
    
    ws = WORKSPACE_DIR
    
    # Tenta como caminho completo primeiro
    full_path = Path(video_path)
    
    # Se não for absoluto, assume que está no workspace
    if not full_path.is_absolute():
        safe_name = sanitize_filename(video_path)
        full_path = ws / safe_name
    
    # Verifica se é um caminho seguro (dentro do workspace)
    if not is_safe_path(str(full_path), ws):
        return False, "Caminho não permitido (possível tentativa de path traversal)"
    
    # Verifica se o arquivo existe
    if not full_path.is_file():
        return False, f"Arquivo não encontrado: {full_path.name}"
    
    # Verifica a extensão
    ext = full_path.suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return False, f"Extensão não permitida: {ext}. Use: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
    
    return True, str(full_path)


def log_security_event(event_type: str, details: str, request_info: dict = None):
    """
    Registra eventos de segurança para auditoria.
    """
    timestamp = datetime.now().isoformat()
    client_ip = request.remote_addr if request else "N/A"
    
    log_entry = f"[SECURITY] [{timestamp}] [{event_type}] IP={client_ip} - {details}"
    
    # Log no console (em produção, usar arquivo de log dedicado)
    print(log_entry)
    
    # Em um ambiente de produção, você poderia:
    # - Escrever em arquivo de log
    # - Enviar para sistema de monitoramento
    # - Armazenar em banco de dados


def emit_log(message, level="info"):
    """Emite uma mensagem de log para o cliente via WebSocket"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {
        "info": "ℹ️",
        "success": "✅", 
        "warning": "⚠️",
        "error": "❌"
    }.get(level, "•")
    
    socketio.emit('log_message', {
        'message': f"[{timestamp}] {prefix} {message}",
        'level': level
    })


def emit_progress(text, value):
    """Emite atualização de progresso para o cliente"""
    app_state["progress"] = value
    app_state["progress_text"] = text
    socketio.emit('progress_update', {
        'text': text,
        'value': value * 100
    })


def list_videos(directory: str = None) -> list:
    """
    Lista vídeos disponíveis no diretório de trabalho.
    Por segurança, só lista arquivos dentro do workspace.
    """
    ws = WORKSPACE_DIR
    
    # Usa o workspace atual se não especificado
    search_dir = ws
    
    # Se um diretório foi especificado, valida se está dentro do workspace
    if directory and directory != ".":
        proposed_dir = Path(directory).resolve()
        if is_safe_path(str(proposed_dir), ws):
            search_dir = proposed_dir
        else:
            log_security_event("PATH_TRAVERSAL_ATTEMPT", f"Tentativa de acesso: {directory}")
            return []  # Retorna lista vazia por segurança
    
    videos = []
    
    # Lista apenas extensões permitidas
    for ext in ALLOWED_VIDEO_EXTENSIONS:
        pattern = f"*{ext}"
        videos.extend(search_dir.glob(pattern))
        # Também busca extensões em maiúsculo
        videos.extend(search_dir.glob(pattern.upper()))
    
    # Remove duplicatas e converte para strings
    videos = sorted(list(set(str(v) for v in videos)))
    
    return videos


# ==================== TRATAMENTO DE ERROS ====================

@app.errorhandler(400)
def bad_request(error):
    """Trata requisições malformadas"""
    return jsonify({"success": False, "error": "Requisição inválida"}), 400


@app.errorhandler(404)
def not_found(error):
    """Trata recursos não encontrados"""
    return jsonify({"success": False, "error": "Recurso não encontrado"}), 404


@app.errorhandler(413)
def request_entity_too_large(error):
    """Trata uploads muito grandes"""
    return jsonify({"success": False, "error": "Arquivo muito grande (máx: 10GB)"}), 413


@app.errorhandler(500)
def internal_error(error):
    """
    Trata erros internos sem expor detalhes sensíveis.
    Os detalhes completos ficam apenas no log do servidor.
    """
    log_security_event("INTERNAL_ERROR", str(error))
    return jsonify({"success": False, "error": "Erro interno do servidor"}), 500


# ==================== ROTAS ====================

# ==================== ROTAS DE AUTENTICAÇÃO ====================

@app.route('/login', methods=['GET'])
def login_page():
    """Página de login"""
    if not AUTH_ENABLED:
        return redirect(url_for('index'))
    
    if session.get('authenticated'):
        return redirect(url_for('index'))
    
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login_submit():
    """Processa o login"""
    if not AUTH_ENABLED:
        return jsonify({'success': True, 'redirect': '/'})
    
    data = request.json or {}
    password = data.get('password', '')
    
    if not password:
        log_security_event("LOGIN_FAILED", "Senha vazia")
        return jsonify({'success': False, 'error': 'Senha não informada'}), 400
    
    # Verifica a senha
    if check_password_hash(ACCESS_PASSWORD_HASH, password):
        session.permanent = True
        session['authenticated'] = True
        session['login_time'] = datetime.now().isoformat()
        log_security_event("LOGIN_SUCCESS", "Login bem-sucedido")
        return jsonify({'success': True, 'redirect': '/'})
    else:
        log_security_event("LOGIN_FAILED", "Senha incorreta")
        return jsonify({'success': False, 'error': 'Senha incorreta'}), 401


@app.route('/logout')
def logout():
    """Faz logout"""
    session.clear()
    log_security_event("LOGOUT", "Usuário deslogado")
    return redirect(url_for('login_page') if AUTH_ENABLED else url_for('index'))


@app.route('/api/auth/status')
def auth_status():
    """Retorna status de autenticação"""
    return jsonify({
        'auth_enabled': AUTH_ENABLED,
        'authenticated': session.get('authenticated', False),
        'login_time': session.get('login_time')
    })


# ==================== ROTAS PRINCIPAIS ====================

@app.route('/')
@login_required
def index():
    """Página principal da aplicação"""
    return render_template('index.html', auth_enabled=AUTH_ENABLED)


@app.route('/api/status')
@api_login_required
def get_status():
    """Retorna o status atual da aplicação"""
    return jsonify({
        "selected_video": app_state["selected_video"],
        "is_processing": app_state["is_processing"],
        "has_api_key": bool(app_state["api_key"]),
        "progress": app_state["progress"],
        "progress_text": app_state["progress_text"]
    })


@app.route('/api/health')
def health_check():
    """Health check para monitoramento e proxies (não exige login)."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# ==================== ROTAS DE CONFIGURAÇÕES (PERSISTENTES) ====================

@app.route('/api/settings', methods=['GET'])
@api_login_required
def get_settings():
    """Retorna todas as configurações salvas"""
    settings = db.get_all_settings()
    # Não expõe a API key completa
    if settings.get('gemini_api_key'):
        key = settings['gemini_api_key']
        settings['gemini_api_key_masked'] = key[:4] + "..." + key[-4:] if len(key) > 8 else "****"
        settings['has_api_key'] = True
    else:
        settings['has_api_key'] = False
    settings.pop('gemini_api_key', None)
    return jsonify(settings)


@app.route('/api/settings', methods=['POST'])
@api_login_required
def save_settings():
    """Salva configurações no banco"""
    data = request.json or {}
    
    allowed_keys = ['workspace_dir', 'whisper_model', 'cut_method', 'language', 'use_ai_correction']
    updated_keys = []

    # API key: tratamento separado para não logar o valor
    if 'gemini_api_key' in data:
        api_key = (data.get('gemini_api_key') or '').strip()
        if api_key:
            if len(api_key) > 200 or not re.match(r'^[A-Za-z0-9_-]+$', api_key):
                return jsonify({"success": False, "error": "API key inválida"}), 400
            db.set_setting('gemini_api_key', api_key)
            app_state["api_key"] = api_key
            os.environ["GEMINI_API_KEY"] = api_key
        updated_keys.append('gemini_api_key')
    
    for key in allowed_keys:
        if key in data:
            value = str(data[key])
            db.set_setting(key, value)
            updated_keys.append(key)
            # Atualiza configurações em memória
            if key in default_settings:
                if key == 'use_ai_correction':
                    default_settings[key] = value.lower() == 'true'
                else:
                    default_settings[key] = value
    
    if updated_keys:
        log_keys = [k for k in updated_keys if k != 'gemini_api_key']
        if log_keys:
            db.add_log("Sistema", "settings_update", "success", f"Configurações atualizadas: {log_keys}")
        else:
            db.add_log("Sistema", "settings_update", "success", "API key atualizada")
    return jsonify({"success": True, "message": "Configurações salvas"})


# ==================== ROTAS DE HISTÓRICO E LOGS ====================

@app.route('/api/history')
@api_login_required
def get_history():
    """Retorna histórico de vídeos processados"""
    limit = request.args.get('limit', 20, type=int)
    history = db.get_video_history(limit)
    return jsonify(history)


@app.route('/api/logs')
@api_login_required
def get_logs():
    """Retorna logs de processamento"""
    limit = request.args.get('limit', 50, type=int)
    logs = db.get_logs(limit)
    return jsonify(logs)


@app.route('/api/logs', methods=['DELETE'])
@api_login_required
def clear_logs():
    """Limpa todos os logs"""
    db.clear_logs()
    return jsonify({"success": True, "message": "Logs limpos"})


@app.route('/api/stats')
@api_login_required
def get_stats():
    """Retorna estatísticas de uso"""
    stats = db.get_stats()
    return jsonify(stats)


@app.route('/api/upload', methods=['POST'])
@api_login_required
def upload_video():
    """
    Faz upload de um vídeo para o workspace.
    Útil quando o usuário seleciona um vídeo de fora da pasta do projeto.
    """
    if 'video' not in request.files:
        return jsonify({"success": False, "error": "Nenhum arquivo enviado"}), 400
    
    file = request.files['video']
    
    if file.filename == '':
        return jsonify({"success": False, "error": "Nome de arquivo vazio"}), 400
    
    # Sanitiza o nome do arquivo
    safe_name = sanitize_filename(file.filename)
    
    if not safe_name:
        return jsonify({"success": False, "error": "Nome de arquivo inválido"}), 400
    
    # Verifica extensão
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({"success": False, "error": f"Extensão não permitida: {ext}"}), 400
    
    # Caminho de destino (usa workspace atual)
    ws = WORKSPACE_DIR
    dest_path = ws / safe_name
    
    # Se já existe, adiciona sufixo
    if dest_path.exists():
        base = dest_path.stem
        counter = 1
        while dest_path.exists():
            dest_path = ws / f"{base}_{counter}{ext}"
            counter += 1
        safe_name = dest_path.name
    
    try:
        # Salva o arquivo
        file.save(str(dest_path))
        
        # Seleciona automaticamente
        app_state["selected_video"] = str(dest_path)
        
        # Log
        file_size = dest_path.stat().st_size / (1024 * 1024)
        db.add_log(safe_name, "upload", "success", f"Tamanho: {file_size:.2f} MB")
        log_security_event("FILE_UPLOAD", f"Arquivo: {safe_name}, Tamanho: {file_size:.2f} MB")
        
        return jsonify({
            "success": True,
            "filename": safe_name,
            "path": str(dest_path),
            "size": dest_path.stat().st_size
        })
        
    except Exception as e:
        log_security_event("UPLOAD_ERROR", str(e))
        return jsonify({"success": False, "error": f"Erro ao salvar: {str(e)}"}), 500


@app.route('/api/videos')
@api_login_required
def get_videos():
    """Lista vídeos disponíveis"""
    directory = request.args.get('directory', '.')
    videos = list_videos(directory)
    
    video_list = []
    for v in videos:
        stat = os.stat(v)
        video_list.append({
            "path": v,
            "name": os.path.basename(v),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
        })
    
    return jsonify(video_list)


@app.route('/api/files')
@api_login_required
def list_files():
    """
    Lista arquivos e pastas em um diretório (estilo explorador).
    Retorna pastas e vídeos separados.
    """
    rel_path = request.args.get('path', '')
    ws = WORKSPACE_DIR
    
    # Constrói caminho seguro
    if rel_path:
        target_dir = ws / rel_path
    else:
        target_dir = ws
    
    # Valida segurança
    if not is_safe_path(str(target_dir), ws):
        return jsonify({"error": "Caminho não permitido"}), 403
    
    if not target_dir.exists():
        return jsonify({"error": "Diretório não encontrado"}), 404
    
    folders = []
    files = []
    
    try:
        for item in sorted(target_dir.iterdir()):
            # Ignora arquivos ocultos
            if item.name.startswith('.'):
                continue
            
            if item.is_dir():
                # Conta itens na pasta
                try:
                    item_count = len(list(item.iterdir()))
                except:
                    item_count = 0
                
                folders.append({
                    "name": item.name,
                    "path": str(item.relative_to(ws)),
                    "type": "folder",
                    "items": item_count
                })
            elif item.is_file() and item.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS:
                stat = item.stat()
                files.append({
                    "name": item.name,
                    "path": str(item.relative_to(ws)),
                    "full_path": str(item),
                    "type": "video",
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "extension": item.suffix.lower()
                })
    except PermissionError:
        return jsonify({"error": "Sem permissão para acessar"}), 403
    
    # Calcula o breadcrumb
    breadcrumb = [{"name": "🏠 Workspace", "path": ""}]
    if rel_path:
        parts = Path(rel_path).parts
        current = ""
        for part in parts:
            current = str(Path(current) / part) if current else part
            breadcrumb.append({"name": part, "path": current})
    
    return jsonify({
        "current_path": rel_path or "",
        "full_path": str(target_dir),
        "breadcrumb": breadcrumb,
        "folders": folders,
        "files": files,
        "total_folders": len(folders),
        "total_files": len(files)
    })


@app.route('/api/files/delete', methods=['POST'])
@api_login_required
def delete_file():
    """Deleta um arquivo de vídeo"""
    data = request.json or {}
    file_path = data.get('path', '')
    
    if not file_path:
        return jsonify({"success": False, "error": "Caminho não informado"}), 400
    
    ws = WORKSPACE_DIR
    
    # Constrói caminho completo
    full_path = ws / file_path
    
    # Valida segurança
    if not is_safe_path(str(full_path), ws):
        log_security_event("DELETE_BLOCKED", f"Tentativa de deletar fora do workspace: {file_path}")
        return jsonify({"success": False, "error": "Caminho não permitido"}), 403
    
    if not full_path.exists():
        return jsonify({"success": False, "error": "Arquivo não encontrado"}), 404
    
    if not full_path.is_file():
        return jsonify({"success": False, "error": "Não é um arquivo"}), 400
    
    # Verifica se é um vídeo
    if full_path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({"success": False, "error": "Apenas vídeos podem ser deletados"}), 400
    
    try:
        file_name = full_path.name
        full_path.unlink()
        
        # Log
        db.add_log(file_name, "delete", "success", f"Arquivo deletado")
        log_security_event("FILE_DELETED", f"Arquivo: {file_name}")
        
        # Se era o vídeo selecionado, limpa a seleção
        if app_state["selected_video"] == str(full_path):
            app_state["selected_video"] = None
        
        return jsonify({"success": True, "message": f"Arquivo '{file_name}' deletado"})
    except Exception as e:
        log_security_event("DELETE_ERROR", str(e))
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/files/folder', methods=['POST'])
@api_login_required
def create_folder():
    """Cria uma nova pasta dentro do workspace"""
    data = request.json or {}
    parent_path = data.get('parent', '')
    folder_name = data.get('name', '').strip()
    
    if not folder_name:
        return jsonify({"success": False, "error": "Nome da pasta não informado"}), 400
    
    # Sanitiza o nome
    folder_name = sanitize_filename(folder_name)
    if not folder_name:
        return jsonify({"success": False, "error": "Nome de pasta inválido"}), 400
    
    # Constrói caminho
    if parent_path:
        target_dir = WORKSPACE_DIR / parent_path / folder_name
    else:
        target_dir = WORKSPACE_DIR / folder_name
    
    # Valida segurança
    if not is_safe_path(str(target_dir)):
        return jsonify({"success": False, "error": "Caminho não permitido"}), 403
    
    if target_dir.exists():
        return jsonify({"success": False, "error": "Pasta já existe"}), 400
    
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        log_security_event("FOLDER_CREATED", f"Pasta criada: {target_dir.relative_to(WORKSPACE_DIR)}")
        return jsonify({
            "success": True, 
            "message": f"Pasta '{folder_name}' criada",
            "path": str(target_dir.relative_to(WORKSPACE_DIR))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/files/folder', methods=['DELETE'])
@api_login_required
def delete_folder():
    """Deleta uma pasta (apenas se estiver vazia ou com flag force)"""
    data = request.json or {}
    folder_path = data.get('path', '')
    force = data.get('force', False)
    
    if not folder_path:
        return jsonify({"success": False, "error": "Caminho não informado"}), 400
    
    full_path = WORKSPACE_DIR / folder_path
    
    # Valida segurança
    if not is_safe_path(str(full_path)):
        return jsonify({"success": False, "error": "Caminho não permitido"}), 403
    
    if not full_path.exists():
        return jsonify({"success": False, "error": "Pasta não encontrada"}), 404
    
    if not full_path.is_dir():
        return jsonify({"success": False, "error": "Não é uma pasta"}), 400
    
    # Não permite deletar pasta 'processados'
    if full_path.name == 'processados' and full_path.parent == WORKSPACE_DIR:
        return jsonify({"success": False, "error": "Pasta 'processados' não pode ser deletada"}), 403
    
    try:
        if force:
            import shutil
            shutil.rmtree(str(full_path))
        else:
            # Verifica se está vazia
            if any(full_path.iterdir()):
                return jsonify({"success": False, "error": "Pasta não está vazia. Use force=true para deletar."}), 400
            full_path.rmdir()
        
        log_security_event("FOLDER_DELETED", f"Pasta deletada: {folder_path}")
        return jsonify({"success": True, "message": f"Pasta deletada"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/files/move', methods=['POST'])
@api_login_required
def move_file():
    """Move um arquivo ou pasta para outro local"""
    data = request.json or {}
    source_path = data.get('source', '')
    dest_folder = data.get('destination', '')
    
    if not source_path:
        return jsonify({"success": False, "error": "Arquivo de origem não informado"}), 400
    
    source = WORKSPACE_DIR / source_path
    
    # Destino: se vazio, move para raiz do workspace
    if dest_folder:
        dest_dir = WORKSPACE_DIR / dest_folder
    else:
        dest_dir = WORKSPACE_DIR
    
    # Valida segurança
    if not is_safe_path(str(source)) or not is_safe_path(str(dest_dir)):
        return jsonify({"success": False, "error": "Caminho não permitido"}), 403
    
    if not source.exists():
        return jsonify({"success": False, "error": "Arquivo de origem não encontrado"}), 404
    
    if not dest_dir.exists() or not dest_dir.is_dir():
        return jsonify({"success": False, "error": "Pasta de destino não existe"}), 404
    
    dest_path = dest_dir / source.name
    
    if dest_path.exists():
        return jsonify({"success": False, "error": "Já existe um item com este nome no destino"}), 400
    
    try:
        import shutil
        shutil.move(str(source), str(dest_path))
        
        log_security_event("FILE_MOVED", f"Movido: {source_path} → {dest_folder or 'raiz'}")
        return jsonify({
            "success": True, 
            "message": f"'{source.name}' movido para '{dest_folder or 'raiz'}'",
            "new_path": str(dest_path.relative_to(WORKSPACE_DIR))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/files/rename', methods=['POST'])
@api_login_required
def rename_file():
    """Renomeia um arquivo ou pasta"""
    data = request.json or {}
    file_path = data.get('path', '')
    new_name = data.get('new_name', '').strip()
    
    if not file_path or not new_name:
        return jsonify({"success": False, "error": "Caminho e novo nome são obrigatórios"}), 400
    
    # Sanitiza o novo nome
    new_name = sanitize_filename(new_name)
    if not new_name:
        return jsonify({"success": False, "error": "Nome inválido"}), 400
    
    source = WORKSPACE_DIR / file_path
    
    if not is_safe_path(str(source)):
        return jsonify({"success": False, "error": "Caminho não permitido"}), 403
    
    if not source.exists():
        return jsonify({"success": False, "error": "Item não encontrado"}), 404
    
    # Mantém a extensão se for arquivo
    if source.is_file() and source.suffix:
        if not new_name.endswith(source.suffix):
            new_name += source.suffix
    
    dest = source.parent / new_name
    
    if dest.exists():
        return jsonify({"success": False, "error": "Já existe um item com este nome"}), 400
    
    try:
        source.rename(dest)
        
        log_security_event("FILE_RENAMED", f"Renomeado: {source.name} → {new_name}")
        return jsonify({
            "success": True, 
            "message": f"Renomeado para '{new_name}'",
            "new_path": str(dest.relative_to(WORKSPACE_DIR))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/files/preview/<path:file_path>')
@api_login_required
def preview_file(file_path):
    """Serve um arquivo de vídeo para preview"""
    from flask import send_file, Response
    
    ws = WORKSPACE_DIR
    full_path = ws / file_path
    
    # Valida segurança
    if not is_safe_path(str(full_path), ws):
        abort(403)
    
    if not full_path.exists() or not full_path.is_file():
        abort(404)
    
    if full_path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        abort(400)
    
    # Determina o MIME type
    mime_types = {
        '.mp4': 'video/mp4',
        '.mov': 'video/quicktime',
        '.mkv': 'video/x-matroska',
        '.avi': 'video/x-msvideo',
        '.webm': 'video/webm',
        '.m4v': 'video/x-m4v'
    }
    mime = mime_types.get(full_path.suffix.lower(), 'video/mp4')
    
    return send_file(str(full_path), mimetype=mime)


# Diretório de cache para thumbnails (na pasta do projeto, não no workspace)
PROJECT_DIR = Path(__file__).parent.resolve()
THUMBNAIL_CACHE_DIR = PROJECT_DIR / '.thumbnails'

# Pasta de fontes customizadas (upload do usuário)
FONTS_DIR = PROJECT_DIR / 'fonts'
ALLOWED_FONT_EXTENSIONS = {'.ttf', '.otf', '.woff', '.woff2'}


@app.route('/api/files/thumbnail/<path:file_path>')
@api_login_required
def get_thumbnail(file_path):
    """
    Gera e retorna thumbnail de um vídeo usando FFmpeg.
    Os thumbnails são cacheados em .thumbnails/
    """
    from flask import send_file
    import subprocess
    import hashlib
    
    ws = WORKSPACE_DIR
    full_path = ws / file_path
    
    # Valida segurança
    if not is_safe_path(str(full_path), ws):
        abort(403)
    
    if not full_path.exists() or not full_path.is_file():
        abort(404)
    
    if full_path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        abort(400)
    
    # Cria diretório de cache se não existir
    THUMBNAIL_CACHE_DIR.mkdir(exist_ok=True)
    
    # Nome do thumbnail baseado no hash do caminho + data de modificação
    file_stat = full_path.stat()
    cache_key = f"{file_path}_{file_stat.st_mtime}"
    thumb_hash = hashlib.md5(cache_key.encode()).hexdigest()
    thumb_path = THUMBNAIL_CACHE_DIR / f"{thumb_hash}.jpg"
    
    # Se thumbnail já existe no cache, retorna
    if thumb_path.exists():
        return send_file(str(thumb_path), mimetype='image/jpeg')
    
    # Gera thumbnail com FFmpeg
    try:
        # Extrai frame em 1 segundo do vídeo, redimensiona para largura 320px
        cmd = [
            'ffmpeg',
            '-i', str(full_path),
            '-ss', '00:00:01',      # Pula para 1 segundo
            '-vframes', '1',         # Extrai 1 frame
            '-vf', 'scale=320:-1',   # Largura 320, altura proporcional
            '-q:v', '3',             # Qualidade (1-31, menor = melhor)
            '-y',                    # Sobrescreve se existir
            str(thumb_path)
        ]
        
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            timeout=10,
            check=False
        )
        
        if thumb_path.exists():
            return send_file(str(thumb_path), mimetype='image/jpeg')
        else:
            # Se falhou, tenta extrair do início do vídeo
            cmd[4] = '00:00:00'
            subprocess.run(cmd, capture_output=True, timeout=10, check=False)
            
            if thumb_path.exists():
                return send_file(str(thumb_path), mimetype='image/jpeg')
    
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        print(f"Erro ao gerar thumbnail: {e}")
    
    # Fallback: placeholder SVG quando FFmpeg não está disponível ou falha
    placeholder_svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
        <rect width="320" height="180" fill="#1a1a2e"/>
        <path d="M120 65v50l40-25-40-25z" fill="#6b6b80"/>
        <text x="160" y="155" text-anchor="middle" fill="#6b6b80" font-size="12" font-family="sans-serif">Vídeo</text>
    </svg>'''
    return Response(placeholder_svg, mimetype='image/svg+xml')


@app.route('/api/fonts/list', methods=['GET'])
@api_login_required
def list_fonts():
    """Lista fontes do sistema (fixas) e fontes customizadas (pasta fonts/)"""
    custom = []
    try:
        FONTS_DIR.mkdir(exist_ok=True)
        for f in sorted(FONTS_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_FONT_EXTENSIONS:
                # Nome para exibição: nome do arquivo sem extensão
                name = f.stem
                custom.append({"name": name, "path": str(f)})
    except Exception:
        pass
    return jsonify({
        "success": True,
        "custom": custom
    })


@app.route('/api/fonts/upload', methods=['POST'])
@api_login_required
def upload_font():
    """Faz upload de uma fonte e salva em fonts/. Retorna o nome da fonte para usar na lista."""
    if 'font' not in request.files and 'file' not in request.files:
        return jsonify({"success": False, "error": "Nenhum arquivo enviado"}), 400
    file = request.files.get('font') or request.files.get('file')
    if not file or file.filename == '':
        return jsonify({"success": False, "error": "Arquivo inválido"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_FONT_EXTENSIONS:
        return jsonify({"success": False, "error": f"Extensão não permitida. Use: {', '.join(ALLOWED_FONT_EXTENSIONS)}"}), 400
    safe_name = sanitize_filename(file.filename)
    if not safe_name:
        return jsonify({"success": False, "error": "Nome de arquivo inválido"}), 400
    try:
        FONTS_DIR.mkdir(exist_ok=True)
        dest = FONTS_DIR / safe_name
        file.save(str(dest))
        # Nome para uso no ASS: nome sem extensão (font family)
        font_display_name = Path(safe_name).stem
        return jsonify({"success": True, "font_name": font_display_name, "filename": safe_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/select_video', methods=['POST'])
@api_login_required
def select_video():
    """Seleciona um vídeo para processamento"""
    data = request.json
    
    if not data:
        return jsonify({"success": False, "error": "Dados inválidos"}), 400
    
    video_path = data.get('path', '')
    
    # Valida o caminho do vídeo
    is_valid, result = validate_video_path(video_path)
    
    if is_valid:
        app_state["selected_video"] = result  # result contém o caminho completo validado
        return jsonify({
            "success": True, 
            "video": os.path.basename(result)
        })
    else:
        log_security_event("INVALID_VIDEO_PATH", f"Caminho: {video_path}, Erro: {result}")
        return jsonify({"success": False, "error": result}), 400


@app.route('/api/clear_selection', methods=['POST'])
@api_login_required
def clear_selection():
    """Remove a seleção de vídeo atual"""
    app_state["selected_video"] = None
    return jsonify({"success": True, "message": "Seleção removida"})


@app.route('/api/validate_api_key', methods=['POST'])
@api_login_required
def validate_api_key():
    """
    Valida se a API key do Gemini está funcionando (chamada mínima ao modelo).
    Usa a key atual ou a enviada no body.
    """
    data = request.json or {}
    api_key = data.get('api_key', '').strip() or app_state.get("api_key", "")
    
    if not api_key:
        return jsonify({"valid": False, "error": "Nenhuma API key configurada"}), 400
    
    try:
        import google.generativeai as genai
    except ImportError:
        return jsonify({"valid": False, "error": "Biblioteca google-generativeai não instalada"}), 400

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        # Chamada mínima para validar a key
        response = model.generate_content("Responda apenas: ok", generation_config={"max_output_tokens": 10})
        if response and response.text:
            return jsonify({"valid": True, "message": "API key válida"})
    except Exception as e:
        err_msg = str(e).strip()
        if "API_KEY_INVALID" in err_msg or "403" in err_msg or "401" in err_msg:
            return jsonify({"valid": False, "error": "API key inválida ou sem permissão"}), 400
        return jsonify({"valid": False, "error": err_msg[:200]}), 400
    
    return jsonify({"valid": False, "error": "Resposta inesperada"}), 400


@app.route('/api/set_api_key', methods=['POST'])
@api_login_required
def set_api_key():
    """
    Configura a API key do Gemini.
    
    Validações de segurança:
    - Verifica formato básico da key
    - Limita tamanho para evitar overflow
    - Remove caracteres perigosos
    """
    data = request.json
    
    if not data:
        return jsonify({"success": False, "error": "Dados inválidos"}), 400
    
    api_key = data.get('api_key', '').strip()
    
    # Validação básica de formato (API keys do Google geralmente são alfanuméricas)
    if not api_key:
        return jsonify({"success": False, "error": "API key não pode estar vazia"}), 400
    
    if len(api_key) > 200:  # API keys do Google não excedem esse tamanho
        return jsonify({"success": False, "error": "API key inválida (muito longa)"}), 400
    
    if not re.match(r'^[A-Za-z0-9_-]+$', api_key):
        return jsonify({"success": False, "error": "API key contém caracteres inválidos"}), 400
    
    # Armazena a key (memória + banco)
    app_state["api_key"] = api_key
    os.environ["GEMINI_API_KEY"] = api_key
    db.set_setting("gemini_api_key", api_key)  # Persiste no banco
    
    # Log de auditoria (sem expor a key completa)
    masked_key = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "****"
    log_security_event("API_KEY_CONFIGURED", f"Key configurada: {masked_key}")
    
    return jsonify({"success": True})


@app.route('/api/reveal_api_key', methods=['GET'])
@api_login_required
def reveal_api_key():
    """
    Retorna a API key atual apenas para exibição/cópia no modal de edição.
    Uso restrito à interface autenticada.
    """
    key = app_state.get("api_key") or db.get_setting("gemini_api_key") or ""
    if not key:
        return jsonify({"api_key": None})
    return jsonify({"api_key": key})


@app.route('/api/process/remove_silence', methods=['POST'])
@api_login_required
def process_remove_silence():
    """Inicia processo de remoção de silêncio"""
    if app_state["is_processing"]:
        return jsonify({"success": False, "error": "Processamento em andamento"}), 400
    
    if not app_state["selected_video"]:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
    
    work_path = ensure_video_and_sidecars_in_processados(app_state["selected_video"])
    if work_path is None:
        return jsonify({"success": False, "error": "Não foi possível copiar o vídeo para a pasta processados"}), 400
    
    data = request.json or {}
    method = data.get('method', 'speech')
    padding = float(data.get('padding', 0.25))
    min_silence = float(data.get('min_silence', 0.5))
    word_threshold = float(data.get('word_threshold', 0.25))
    
    def process():
        video_path = app_state["selected_video"]
        app_state["cancel_requested"] = False
        try:
            app_state["is_processing"] = True
            emit_log("Iniciando remoção de silêncio...", "info")
            emit_progress("Carregando módulos de IA...", 0.1)
            
            from remove_silence import remover_silencio
            
            output_path = get_output_path(video_path, '_cut')
            get_cancelled = lambda: app_state.get("cancel_requested", False)
            
            emit_progress("Analisando áudio...", 0.3)
            emit_log(f"Usando método de corte: {method.upper()} (Margem: {padding}s, Silêncio min: {min_silence}s)", "info")
            
            start_time = time.time()
            success = remover_silencio(video_path, output_path, method=method, min_duration=min_silence, padding=padding, word_threshold=word_threshold, get_cancelled=get_cancelled)
            duration = time.time() - start_time
            
            if app_state.get("cancel_requested"):
                emit_log("Processamento cancelado pelo usuário.", "warning")
                socketio.emit('process_complete', {'success': False, 'error': 'Cancelado pelo usuário'})
                return
            if success:
                emit_progress("Concluído!", 1.0)
                emit_log(f"✅ Vídeo cortado salvo em: {os.path.basename(output_path)}", "success")
                
                # Registra no banco
                file_size = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0
                db.add_video_history(video_path, output_path, "remove_silence", True, file_size)
                db.add_log(os.path.basename(video_path), "remove_silence", "success", f"Método: {method}", duration)
                
                app_state["selected_video"] = output_path
                socketio.emit('process_complete', {
                    'success': True,
                    'output': output_path,
                    'filename': os.path.basename(output_path),
                    'selected_video': output_path
                })
            else:
                emit_log("❌ Falha no processamento", "error")
                db.add_log(os.path.basename(video_path), "remove_silence", "failed", "Falha no processamento")
                socketio.emit('process_complete', {'success': False, 'error': 'Falha no processamento'})
                
        except Exception as e:
            emit_log(f"❌ Erro: {str(e)}", "error")
            db.add_log(os.path.basename(video_path), "remove_silence", "error", str(e))
            socketio.emit('process_complete', {'success': False, 'error': str(e)})
        finally:
            app_state["is_processing"] = False
            app_state["cancel_requested"] = False
            emit_progress("Aguardando início...", 0)
    
    threading.Thread(target=process, daemon=True).start()
    return jsonify({"success": True, "message": "Processamento iniciado"})


@app.route('/api/process/preview_subtitle', methods=['POST'])
@api_login_required
def preview_subtitle():
    """Gera um preview da legenda com as configurações atuais"""
    data = request.json or {}
    raw_path = data.get("video_path") or app_state.get("selected_video")
    if not raw_path:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
    raw_path = str(raw_path).strip()
    is_valid, video_path = validate_video_path(raw_path)
    if not is_valid:
        return jsonify({"success": False, "error": str(video_path)}), 400
    video_path = str(video_path)
    
    # Configurações de estilo
    highlight_color = data.get('highlight_color')
    text_color = data.get('text_color')
    outline_color = data.get('outline_color')
    highlight_width = float(data.get('highlight_width', 5.0))
    outline_width = float(data.get('outline_width', 1.5))
    font_name = data.get('font_name', 'Prohibition')
    font_size = float(data.get('font_size', 10))
    sub_x_percent = data.get('sub_x_percent')
    sub_y_percent = data.get('sub_y_percent')
    subtitle_model = data.get('subtitle_model', 'highlights')
    font_opacity = data.get('font_opacity')
    font_weight = data.get('font_weight', 'bold')
    font_case = data.get('font_case', 'Tt')
    bg_enabled = data.get('bg_enabled', False)
    bg_color = data.get('bg_color')
    bg_opacity = data.get('bg_opacity')
    if sub_x_percent is not None:
        sub_x_percent = float(sub_x_percent)
    if sub_y_percent is not None:
        sub_y_percent = float(sub_y_percent)
    
    try:
        from auto_caption import gerar_ass_capcut
        import subprocess
        from flask import send_file
        
        # 1. Gera thumbnail temporária (ou usa cache)
        THUMBNAIL_CACHE_DIR.mkdir(exist_ok=True)
        import hashlib
        file_stat = Path(video_path).stat()
        cache_key = f"{video_path}_{file_stat.st_mtime}"
        thumb_hash = hashlib.md5(cache_key.encode()).hexdigest()
        thumb_path = THUMBNAIL_CACHE_DIR / f"{thumb_hash}.jpg"
        
        if not thumb_path.exists():
            # Gera thumbnail
            cmd = [
                'ffmpeg', '-i', video_path, '-ss', '00:00:05',
                '-vframes', '1', '-vf', 'scale=640:-1', '-q:v', '3', '-y', str(thumb_path)
            ]
            result = subprocess.run(cmd, capture_output=True, check=False, timeout=15)
            if not thumb_path.exists():
                err = (result.stderr or b'').decode('utf-8', errors='ignore').strip()
                if not err:
                    err = "FFmpeg não retornou saída. Verifique se o FFmpeg está instalado e no PATH."
                return jsonify({
                    "success": False,
                    "error": "Não foi possível extrair frame do vídeo. " + (err[:200] if len(err) > 200 else err)
                }), 500
             
        # 2. Cria ASS de preview
        preview_ass = THUMBNAIL_CACHE_DIR / f"preview_{thumb_hash}.ass"
        preview_img = THUMBNAIL_CACHE_DIR / f"preview_{thumb_hash}.jpg"
        
        # Mock de segmentos para gerar o ASS (precisa de "start"/"end" no segmento para gerar_ass_capcut)
        mock_segments = [{
            "start": 0.0,
            "end": 1.5,
            "words": [
                {"word": "PREVIEW", "start": 0.0, "end": 0.5},
                {"word": "DA", "start": 0.5, "end": 0.8},
                {"word": "LEGENDA", "start": 0.8, "end": 1.5}
            ]
        }]
        
        gerar_ass_capcut(
            mock_segments,
            str(preview_ass),
            highlight_color=highlight_color,
            text_color=text_color,
            outline_color=outline_color,
            highlight_width=highlight_width,
            outline_width=outline_width,
            font_name=font_name,
            font_size=font_size,
            sub_x_percent=sub_x_percent,
            sub_y_percent=sub_y_percent,
            play_res_x=640,
            play_res_y=360,
            subtitle_model=subtitle_model,
            font_opacity=font_opacity,
            font_weight=font_weight,
            font_case=font_case,
            bg_enabled=bg_enabled,
            bg_color=bg_color,
            bg_opacity=bg_opacity
        )

        # 3. Queima a legenda na imagem (frame do vídeo + ASS)
        # Caminho do ASS em temp sem espaços para o filtro do ffmpeg não quebrar
        import tempfile
        import shutil
        with tempfile.NamedTemporaryFile(suffix='.ass', delete=False) as fass:
            ass_temp = fass.name
        shutil.copy2(str(preview_ass), ass_temp)
        try:
            # Path com / para o filtro (evita escape de \ no Windows)
            ass_for_filter = ass_temp.replace("\\", "/")
            # -loop 1: imagem vira "vídeo"; -t 0.1: frame no tempo 0 (legenda visível)
            cmd_burn = [
                'ffmpeg', '-y',
                '-loop', '1', '-i', str(thumb_path),
                '-vf', f"subtitles='{ass_for_filter}'",
                '-t', '0.1',
                '-vframes', '1',
                str(preview_img)
            ]
            result = subprocess.run(cmd_burn, capture_output=True, check=False, timeout=15)
            if not preview_img.exists():
                err = (result.stderr or b'').decode('utf-8', errors='ignore').strip()
                return jsonify({
                    "success": False,
                    "error": "Não foi possível queimar a legenda na imagem. " + (err[-400:] if len(err) > 400 else err)
                }), 500
            return send_file(str(preview_img), mimetype='image/jpeg')
        finally:
            try:
                Path(ass_temp).unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/process/add_subtitles', methods=['POST'])
@api_login_required
def process_add_subtitles():
    """Inicia processo de adicionar legendas"""
    if app_state["is_processing"]:
        return jsonify({"success": False, "error": "Processamento em andamento"}), 400
    
    if not app_state["selected_video"]:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
    
    work_path = ensure_video_and_sidecars_in_processados(app_state["selected_video"])
    if work_path is None:
        return jsonify({"success": False, "error": "Não foi possível copiar o vídeo para a pasta processados"}), 400
    
    data = request.json or {}
    model = data.get('model', 'small')
    language = data.get('language', 'pt')
    use_ai = data.get('use_ai', True)
    
    # Configurações de estilo
    highlight_color = data.get('highlight_color')
    text_color = data.get('text_color')
    outline_color = data.get('outline_color')
    highlight_width = float(data.get('highlight_width', 5.0))
    outline_width = float(data.get('outline_width', 1.5))
    font_name = data.get('font_name', 'Prohibition')
    font_size = float(data.get('font_size', 10))
    subtitle_model = data.get('subtitle_model', 'highlights')
    sub_x_percent = data.get('sub_x_percent')
    sub_y_percent = data.get('sub_y_percent')
    font_opacity = data.get('font_opacity')
    font_weight = data.get('font_weight', 'bold')
    font_case = data.get('font_case', 'Tt')
    bg_color = data.get('bg_color')
    bg_opacity = data.get('bg_opacity')
    preview_mode = data.get('preview_mode', False)
    if sub_x_percent is not None:
        sub_x_percent = float(sub_x_percent)
    if sub_y_percent is not None:
        sub_y_percent = float(sub_y_percent)
    
    def process():
        video_path = app_state["selected_video"]
        app_state["cancel_requested"] = False
        try:
            app_state["is_processing"] = True
            emit_log("Iniciando processo de legendagem...", "info")
            emit_progress("Carregando Whisper...", 0.1)
            
            from auto_caption import processar_legenda_completo
            
            output_path = get_output_path(video_path, '_legendado')
            
            gemini_key = app_state["api_key"] if use_ai else None
            
            emit_progress("Transcrevendo áudio...", 0.3)
            emit_log(f"Usando modelo Whisper: {model}", "info")
            
            start_time = time.time()
            result_path = processar_legenda_completo(
                video_path,
                output_path,
                model_name=model,
                language=language,
                gemini_key=gemini_key,
                highlight_color=highlight_color,
                text_color=text_color,
                outline_color=outline_color,
                highlight_width=highlight_width,
                outline_width=outline_width,
                font_name=font_name,
                font_size=font_size,
                subtitle_model=subtitle_model,
                sub_x_percent=sub_x_percent,
                sub_y_percent=sub_y_percent,
                font_opacity=font_opacity,
                font_weight=font_weight,
                font_case=font_case,
                bg_color=bg_color,
                bg_opacity=bg_opacity,
                only_generate=preview_mode
            )
            duration = time.time() - start_time
            
            if preview_mode:
                emit_progress("Legendas geradas para revisão!", 1.0)
                emit_log(f"✅ Legendas geradas. Pronto para edição.", "success")
                socketio.emit('subtitle_generated', {
                    'success': True,
                    'video_path': video_path
                })
            else:
                emit_progress("Concluído!", 1.0)
                emit_log(f"✅ Vídeo legendado salvo em: {os.path.basename(output_path)}", "success")
                
                # Registra no banco
                file_size = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0
                db.add_video_history(video_path, output_path, "add_subtitles", True, file_size)
                db.add_log(os.path.basename(video_path), "add_subtitles", "success", f"Modelo: {model}, Idioma: {language}", duration)
                
                app_state["selected_video"] = output_path
                socketio.emit('process_complete', {
                    'success': True,
                    'output': output_path,
                    'filename': os.path.basename(output_path),
                    'selected_video': output_path
                })
            
        except Exception as e:
            emit_log(f"❌ Erro: {str(e)}", "error")
            db.add_log(os.path.basename(video_path), "add_subtitles", "error", str(e))
            socketio.emit('process_complete', {'success': False, 'error': str(e)})
        finally:
            app_state["is_processing"] = False
            app_state["cancel_requested"] = False
            emit_progress("Aguardando início...", 0)
    
    threading.Thread(target=process, daemon=True).start()
    return jsonify({"success": True, "message": "Processamento iniciado"})


@app.route('/api/subtitles/get', methods=['GET'])
@api_login_required
def get_subtitles():
    """Retorna os segmentos de legenda (JSON) do vídeo selecionado"""
    if not app_state["selected_video"]:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
        
    video_path = app_state["selected_video"]
    base, _ = os.path.splitext(video_path)
    json_path = f"{base}.json"
    
    if not os.path.exists(json_path):
        return jsonify({"success": False, "error": "Legendas não encontradas. Gere primeiro."}), 404
        
    try:
        from auto_caption import carregar_segmentos_json
        segments = carregar_segmentos_json(json_path)
        return jsonify({"success": True, "segments": segments})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/subtitles/save', methods=['POST'])
@api_login_required
def save_subtitles():
    """Salva segmentos editados e regenera o ASS"""
    if not app_state["selected_video"]:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
        
    data = request.json or {}
    segments = data.get('segments', [])
    
    # Configurações de estilo (opcionais, usa padrão se não vier)
    highlight_color = data.get('highlight_color')
    text_color = data.get('text_color')
    outline_color = data.get('outline_color')
    highlight_width = float(data.get('highlight_width', 5.0))
    outline_width = float(data.get('outline_width', 1.5))
    font_name = data.get('font_name', 'Prohibition')
    font_size = float(data.get('font_size', 10))
    subtitle_model = data.get('subtitle_model', 'highlights')
    sub_x_percent = data.get('sub_x_percent')
    sub_y_percent = data.get('sub_y_percent')
    font_opacity = data.get('font_opacity')
    font_weight = data.get('font_weight', 'bold')
    font_case = data.get('font_case', 'Tt')
    bg_enabled = data.get('bg_enabled', False)
    bg_color = data.get('bg_color')
    bg_opacity = data.get('bg_opacity')
    if sub_x_percent is not None:
        sub_x_percent = float(sub_x_percent)
    if sub_y_percent is not None:
        sub_y_percent = float(sub_y_percent)
    
    video_path = app_state["selected_video"]
    base, _ = os.path.splitext(video_path)
    json_path = f"{base}.json"
    ass_path = f"{base}.ass"
    
    try:
        from auto_caption import salvar_segmentos_json, gerar_ass_capcut
        
        # 1. Salva JSON atualizado
        salvar_segmentos_json(segments, json_path)
        
        # 2. Regenera ASS
        gerar_ass_capcut(
            segments,
            ass_path,
            highlight_color=highlight_color,
            text_color=text_color,
            outline_color=outline_color,
            highlight_width=highlight_width,
            outline_width=outline_width,
            font_name=font_name,
            font_size=font_size,
            sub_x_percent=sub_x_percent,
            sub_y_percent=sub_y_percent,
            subtitle_model=subtitle_model,
            font_opacity=font_opacity,
            font_weight=font_weight,
            font_case=font_case,
            bg_enabled=bg_enabled,
            bg_color=bg_color,
            bg_opacity=bg_opacity
        )
        
        return jsonify({"success": True, "message": "Legendas salvas e atualizadas"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/subtitles/burn', methods=['POST'])
@api_login_required
def burn_subtitles():
    """Queima a legenda atual (.ass) no vídeo"""
    if app_state["is_processing"]:
        return jsonify({"success": False, "error": "Processamento em andamento"}), 400
        
    if not app_state["selected_video"]:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
        
    video_path = app_state["selected_video"]
    base, _ = os.path.splitext(video_path)
    ass_path = f"{base}.ass"
    
    if not os.path.exists(ass_path):
        return jsonify({"success": False, "error": "Arquivo de legenda (.ass) não encontrado"}), 404
        
    def process():
        try:
            app_state["is_processing"] = True
            emit_log("Iniciando queima de legendas...", "info")
            emit_progress("Renderizando vídeo...", 0.1)
            
            from auto_caption import queimar_legenda
            
            output_path = get_output_path(video_path, '_legendado')
            start_time = time.time()
            
            queimar_legenda(video_path, ass_path, output_path)
            
            duration = time.time() - start_time
            
            emit_progress("Concluído!", 1.0)
            emit_log(f"✅ Vídeo legendado salvo em: {os.path.basename(output_path)}", "success")
            
            # Registra no banco
            file_size = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0
            db.add_video_history(video_path, output_path, "burn_subtitles", True, file_size)
            db.add_log(os.path.basename(video_path), "burn_subtitles", "success", f"Manual Burn", duration)
            
            socketio.emit('process_complete', {
                'success': True,
                'output': output_path,
                'filename': os.path.basename(output_path)
            })
            
        except Exception as e:
            emit_log(f"❌ Erro: {str(e)}", "error")
            db.add_log(os.path.basename(video_path), "burn_subtitles", "error", str(e))
            socketio.emit('process_complete', {'success': False, 'error': str(e)})
        finally:
            app_state["is_processing"] = False
            emit_progress("Aguardando início...", 0)
            
    threading.Thread(target=process, daemon=True).start()
    return jsonify({"success": True, "message": "Renderização iniciada"})


@app.route('/api/process/full', methods=['POST'])
@api_login_required
def process_full():
    """Inicia processo completo (corte + legendas)"""
    if app_state["is_processing"]:
        return jsonify({"success": False, "error": "Processamento em andamento"}), 400
    
    if not app_state["selected_video"]:
        return jsonify({"success": False, "error": "Nenhum vídeo selecionado"}), 400
    
    work_path = ensure_video_and_sidecars_in_processados(app_state["selected_video"])
    if work_path is None:
        return jsonify({"success": False, "error": "Não foi possível copiar o vídeo para a pasta processados"}), 400
    
    data = request.json or {}
    model = data.get('model', 'small')
    language = data.get('language', 'pt')
    cut_method = data.get('cut_method', 'speech')
    use_ai = data.get('use_ai', True)
    padding = float(data.get('padding', 0.25))
    min_silence = float(data.get('min_silence', 0.5))
    word_threshold = float(data.get('word_threshold', 0.25))
    
    # Configurações de estilo (mesmas do preview e add_subtitles)
    highlight_color = data.get('highlight_color')
    text_color = data.get('text_color')
    outline_color = data.get('outline_color')
    highlight_width = float(data.get('highlight_width', 5.0))
    outline_width = float(data.get('outline_width', 1.5))
    font_name = data.get('font_name', 'Prohibition')
    font_size = float(data.get('font_size', 10))
    subtitle_model = data.get('subtitle_model', 'highlights')
    sub_x_percent = data.get('sub_x_percent')
    sub_y_percent = data.get('sub_y_percent')
    font_opacity = data.get('font_opacity')
    font_weight = data.get('font_weight', 'bold')
    font_case = data.get('font_case', 'Tt')
    bg_enabled = data.get('bg_enabled', False)
    bg_color = data.get('bg_color')
    bg_opacity = data.get('bg_opacity')
    if sub_x_percent is not None:
        sub_x_percent = float(sub_x_percent)
    if sub_y_percent is not None:
        sub_y_percent = float(sub_y_percent)
    
    def process():
        video_path = app_state["selected_video"]
        app_state["cancel_requested"] = False
        start_time = time.time()
        
        try:
            app_state["is_processing"] = True
            emit_log("🚀 Iniciando processo completo...", "info")
            emit_progress("Carregando módulos...", 0.05)
            
            from remove_silence import remover_silencio
            from auto_caption import processar_legenda_completo
            
            get_cancelled = lambda: app_state.get("cancel_requested", False)
            
            # Passo 1: Cortar silêncio
            emit_log(f"📌 Passo 1/2: Removendo silêncio (Margem: {padding}s)...", "info")
            emit_progress("Analisando áudio...", 0.2)
            
            cut_path = get_output_path(video_path, '_cut')
            success = remover_silencio(video_path, cut_path, method=cut_method, min_duration=min_silence, padding=padding, word_threshold=word_threshold, get_cancelled=get_cancelled)
            
            if get_cancelled():
                emit_log("Processamento cancelado pelo usuário.", "warning")
                socketio.emit('process_complete', {'success': False, 'error': 'Cancelado pelo usuário'})
                return
            
            video_to_caption = cut_path if success else video_path
            
            # Passo 2: Legendar (mesmas opções do modal Configurar Legendas)
            emit_log("📌 Passo 2/2: Gerando legendas...", "info")
            emit_progress("Transcrevendo com Whisper...", 0.5)
            
            final_path = get_output_path(video_path, '_final')
            gemini_key = app_state["api_key"] if use_ai else None
            
            processar_legenda_completo(
                video_to_caption,
                final_path,
                model_name=model,
                language=language,
                gemini_key=gemini_key,
                highlight_color=highlight_color,
                text_color=text_color,
                outline_color=outline_color,
                highlight_width=highlight_width,
                outline_width=outline_width,
                font_name=font_name,
                font_size=font_size,
                subtitle_model=subtitle_model,
                sub_x_percent=sub_x_percent,
                sub_y_percent=sub_y_percent,
                font_opacity=font_opacity,
                font_weight=font_weight,
                font_case=font_case,
                bg_enabled=bg_enabled,
                bg_color=bg_color,
                bg_opacity=bg_opacity,
                only_generate=False
            )
            
            duration = time.time() - start_time
            
            emit_progress("Concluído!", 1.0)
            emit_log(f"✅ Processo completo! Salvo em: {os.path.basename(final_path)}", "success")
            
            # Registra no banco
            file_size = os.path.getsize(final_path) / (1024 * 1024) if os.path.exists(final_path) else 0
            db.add_video_history(video_path, final_path, "full_process", True, file_size)
            db.add_log(os.path.basename(video_path), "full_process", "success", 
                      f"Modelo: {model}, Método: {cut_method}, IA: {use_ai}", duration)
            
            app_state["selected_video"] = final_path
            socketio.emit('process_complete', {
                'success': True,
                'output': final_path,
                'filename': os.path.basename(final_path),
                'selected_video': final_path
            })
            
        except Exception as e:
            emit_log(f"❌ Erro: {str(e)}", "error")
            db.add_log(os.path.basename(video_path), "full_process", "error", str(e))
            socketio.emit('process_complete', {'success': False, 'error': str(e)})
        finally:
            app_state["is_processing"] = False
            app_state["cancel_requested"] = False
            emit_progress("Aguardando início...", 0)
    
    threading.Thread(target=process, daemon=True).start()
    return jsonify({"success": True, "message": "Processamento iniciado"})


@app.route('/api/process/cancel', methods=['POST'])
@api_login_required
def process_cancel():
    """Sinaliza cancelamento: a thread em execução deve parar (get_cancelled) e o estado é limpo."""
    app_state["cancel_requested"] = True
    app_state["is_processing"] = False
    emit_progress("Aguardando início...", 0)
    return jsonify({"success": True, "message": "Processamento cancelado"})


# ==================== SOCKETIO EVENTS ====================

@socketio.on('connect')
def handle_connect():
    """Handler de conexão WebSocket"""
    emit('status', {
        'selected_video': app_state["selected_video"],
        'is_processing': app_state["is_processing"],
        'has_api_key': bool(app_state["api_key"])
    })


# ==================== MAIN ====================

def get_local_ip():
    """Obtém o IP local da máquina para acesso na rede"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    """Inicia o servidor web"""
    port = int(os.environ.get('PORT', 3001))
    host = os.environ.get('HOST', '0.0.0.0')
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    
    local_ip = get_local_ip()
    
    print("\n" + "="*65)
    print("🎬 AUTO VIDEO EDITOR - Interface Web")
    current_ws = WORKSPACE_DIR
    
    print("="*65)
    print(f"\n🌐 Acesso Local:     http://localhost:{port}")
    print(f"🌐 Acesso na Rede:   http://{local_ip}:{port}")
    print(f"📁 Workspace:        {current_ws}")
    
    print("\n" + "-"*65)
    print("🔐 AUTENTICAÇÃO:")
    print("-"*65)
    if AUTH_ENABLED:
        print("   ✅ HABILITADA - Acesso protegido por senha")
        print(f"   ⏱️  Sessão expira em: {SESSION_LIFETIME_HOURS} horas")
        print("   🌍 Pode expor na internet (com HTTPS recomendado)")
    else:
        print("   ⚠️  DESABILITADA - Acesso livre")
        print("   📝 Para habilitar, defina ACCESS_PASSWORD no .env")
        print("   🏠 Recomendado apenas para uso local/rede interna")
    print("-"*65)
    
    print("\n💡 Configurações:")
    print(f"   • Secret Key: {'[PERSONALIZADA]' if os.environ.get('FLASK_SECRET_KEY') else '[GERADA AUTOMATICAMENTE]'}")
    print("   • Formatos suportados:", ", ".join(ALLOWED_VIDEO_EXTENSIONS))
    print("\n" + "="*65 + "\n")
    
    # Em desenvolvimento, podemos usar debug mode
    # Em produção, use gunicorn ou outro WSGI server
    socketio.run(
        app, 
        host=host, 
        port=port, 
        debug=debug, 
        allow_unsafe_werkzeug=True  # Apenas para desenvolvimento
    )


if __name__ == "__main__":
    main()

