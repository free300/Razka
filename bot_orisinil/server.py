"""
Openclaw Server — Manager ringan, semua MCP di luar
"""

import os
import json
import time
import types
import requests
import importlib
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
app = Flask(__name__)
CORS(app)

# =================== CONFIG ===================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase connected")
    except Exception as e:
        print(f"⚠️ Supabase error: {e}")

WORKSPACE    = "/tmp/openclaw"
MEMORY_FILE  = f"{WORKSPACE}/memory.md"
LOG_FILE     = f"{WORKSPACE}/log.md"
SKILLS_DIR   = f"{WORKSPACE}/skills"
TASKS_FILE   = f"{WORKSPACE}/tasks.json"
BOTS_DIR     = f"{WORKSPACE}/bots"
MCP_DIR      = f"{WORKSPACE}/mcp"

for d in [WORKSPACE, SKILLS_DIR, BOTS_DIR, MCP_DIR]:
    os.makedirs(d, exist_ok=True)

for f, default in [
    (MEMORY_FILE, "# Memory Openclaw\n\n"),
    (LOG_FILE,    "# Log Aktivitas\n\n"),
    (TASKS_FILE,  "[]"),
]:
    if not os.path.exists(f):
        with open(f, "w") as fp:
            fp.write(default)

# =================== LOGGING ===================

def tulis_log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line.strip())

def baca_log():
    with open(LOG_FILE, "r") as f:
        return f.read()[-4000:]

# =================== DYNAMIC MCP LOADER ===================

_mcp_registry = {}  # name -> {"module": ..., "tools": [...]}

def load_mcp(name):
    """Load MCP dari file .py di /tmp/openclaw/mcp/"""
    path = f"{MCP_DIR}/{name}.py"
    if not os.path.exists(path):
        # Coba load dari Supabase
        if supabase:
            try:
                res = supabase.table("mcps").select("*").eq("name", name).execute()
                if res.data:
                    with open(path, "w") as f:
                        f.write(res.data[0]["code"])
                    tulis_log(f"✅ MCP '{name}' loaded dari Supabase")
                else:
                    return None
            except Exception as e:
                tulis_log(f"MCP load error: {e}")
                return None
        else:
            return None

    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mcp_registry[name] = mod
        tulis_log(f"✅ MCP '{name}' loaded")
        return mod
    except Exception as e:
        tulis_log(f"❌ MCP '{name}' load error: {e}")
        return None

def get_mcp(name):
    """Ambil MCP yang sudah loaded, atau load dulu"""
    if name in _mcp_registry:
        return _mcp_registry[name]
    return load_mcp(name)

def install_mcp(name, code, description=""):
    """Install MCP baru — simpan ke file + Supabase"""
    try:
        path = f"{MCP_DIR}/{name}.py"
        with open(path, "w") as f:
            f.write(code)
        # Test apakah bisa di-import
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mcp_registry[name] = mod
        # Simpan ke Supabase
        if supabase:
            try:
                supabase.table("mcps").upsert({
                    "name": name, "code": code, "description": description
                }).execute()
            except Exception:
                pass
        tulis_log(f"✅ MCP '{name}' installed")
        return True, f"✅ MCP '{name}' berhasil diinstall!"
    except Exception as e:
        tulis_log(f"❌ MCP '{name}' install error: {e}")
        return False, f"❌ MCP '{name}' gagal: {str(e)}"

def list_mcps():
    """List semua MCP yang terinstall"""
    mcps = []
    for fname in os.listdir(MCP_DIR):
        if fname.endswith(".py"):
            mcps.append(fname.replace(".py", ""))
    return mcps

def load_all_mcps():
    """Load semua MCP saat startup"""
    count = 0
    # Dari file lokal
    for fname in os.listdir(MCP_DIR):
        if fname.endswith(".py"):
            name = fname.replace(".py", "")
            if name not in _mcp_registry:
                load_mcp(name)
                count += 1
    # Dari Supabase
    if supabase:
        try:
            res = supabase.table("mcps").select("name, code").execute()
            if res.data:
                for row in res.data:
                    name = row["name"]
                    path = f"{MCP_DIR}/{name}.py"
                    if not os.path.exists(path):
                        with open(path, "w") as f:
                            f.write(row["code"])
                        load_mcp(name)
                        count += 1
        except Exception as e:
            tulis_log(f"Load MCPs Supabase error: {e}")
    if count:
        tulis_log(f"✅ {count} MCPs loaded")

def call_mcp_function(mcp_name, func_name, args):
    """Panggil fungsi dari MCP yang sudah diinstall"""
    mod = get_mcp(mcp_name)
    if not mod:
        return f"❌ MCP '{mcp_name}' belum diinstall. Ketik: 'install MCP {mcp_name}'"
    func = getattr(mod, func_name, None)
    if not func:
        return f"❌ Fungsi '{func_name}' tidak ada di MCP '{mcp_name}'"
    try:
        if isinstance(args, dict):
            return func(**args)
        return func(args)
    except Exception as e:
        return f"❌ Error MCP {mcp_name}.{func_name}: {str(e)}"

# =================== MULTI-PROVIDER LLM ===================

LLM_PROVIDERS = [
    {
        "name": "groq",
        "env": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "headers": {},
    },
    {
        "name": "groq_reasoning",
        "env": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": ["qwq-32b", "deepseek-r1-distill-llama-70b"],
        "headers": {},
        "reasoning": True,
    },
    {
        "name": "gemini",
        "env": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models": ["gemini-2.0-flash", "gemini-2.0-flash-thinking-exp"],
        "headers": {},
    },
    {
        "name": "openrouter",
        "env": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": ["deepseek/deepseek-r1:free", "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwq-32b:free"],
        "headers": {
            "HTTP-Referer": "https://huggingface.co/spaces/bakulkrupuk2025/openclaw-neo-bot",
            "X-Title": "OpenClaw"
        },
    },
    {
        "name": "cerebras",
        "env": "CEREBRAS_API_KEY",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models": ["llama-3.3-70b", "llama3.1-8b"],
        "headers": {},
    },
    {
        "name": "together",
        "env": "TOGETHER_API_KEY",
        "url": "https://api.together.xyz/v1/chat/completions",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo-Free", "deepseek-ai/DeepSeek-R1-Distill-Llama-70B-free"],
        "headers": {},
    },
    {
        "name": "mistral",
        "env": "MISTRAL_API_KEY",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models": ["mistral-small-latest"],
        "headers": {},
    },
    {
        "name": "cohere",
        "env": "COHERE_API_KEY",
        "url": "https://api.cohere.com/compatibility/v1/chat/completions",
        "models": ["command-r-plus"],
        "headers": {},
    },
]

_cooldown = {}

def _cooldown_set(name):
    _cooldown[name] = time.time() + 60

def _cooldown_ok(name):
    if name in _cooldown:
        if time.time() < _cooldown[name]:
            return False
        del _cooldown[name]
    return True

def get_active_providers(reasoning=False):
    """Rotasi multi-key otomatis — support sampai 5 key per provider.
    Contoh: GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 dst.
    Key yang kena rate limit → cooldown 60s → pakai key berikutnya.
    """
    result = []
    for p in LLM_PROVIDERS:
        if reasoning and not p.get("reasoning"):
            continue
        base_env = p["env"]
        for i in range(1, 6):  # support sampai 5 key per provider
            env_name = base_env if i == 1 else f"{base_env}_{i}"
            key = os.environ.get(env_name, "")
            if not key:
                continue
            slot = p["name"] if i == 1 else f"{p['name']}_{i}"
            if not _cooldown_ok(slot):
                continue  # lagi cooldown, skip ke key berikutnya
            entry = dict(p)
            entry["name"]    = slot
            entry["api_key"] = key
            result.append(entry)
    return result

REASONING_WORDS = [
    "analisis","strategi","kenapa","why","jelaskan","explain",
    "bagaimana","compare","bandingkan","evaluasi","pertimbangkan",
    "rekomendasi","solusi","rencana","planning","pendapat","pikir",
    "perbedaan","keuntungan","kekurangan","review","audit","riset mendalam"
]

def needs_reasoning(text):
    t = text.lower()
    return len(text) > 100 or any(w in t for w in REASONING_WORDS)

def call_llm(messages, tools=None, max_tokens=2048, temperature=0.5, reasoning=False):
    providers = get_active_providers(reasoning=reasoning)
    if not providers:
        providers = get_active_providers(reasoning=False)
    if not providers:
        raise Exception("❌ Tidak ada API key! Ketik 'cek setup' untuk panduan.")

    for p in providers:
        for model in p["models"]:
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {p['api_key']}",
                    **p["headers"]
                }
                payload = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if tools and not reasoning:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                r = requests.post(p["url"], headers=headers, json=payload, timeout=45)
                if r.status_code == 200:
                    tulis_log(f"{'🧠' if reasoning else '✅'} LLM: {p['name']}/{model}")
                    return r.json(), p["name"]
                elif r.status_code in (429, 503):
                    _cooldown_set(p["name"])
                    break
            except requests.Timeout:
                continue
            except Exception as e:
                tulis_log(f"LLM error {p['name']}: {e}")

    raise Exception("❌ Semua provider gagal.")

# =================== MEMORY ===================

def baca_memory():
    with open(MEMORY_FILE) as f:
        return f.read()

def tulis_memory(info):
    with open(MEMORY_FILE, "a") as f:
        f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {info}\n")
    if supabase:
        try:
            supabase.table("memory").insert({"info": info}).execute()
        except Exception:
            pass

# =================== SKILLS ===================

def load_skills():
    skills = {}
    for name in os.listdir(SKILLS_DIR):
        path = f"{SKILLS_DIR}/{name}/SKILL.md"
        if os.path.exists(path):
            with open(path) as f:
                skills[name] = f.read()
    return skills

def skills_for_prompt():
    skills = load_skills()
    if not skills:
        return ""
    lines = ["\n## Skills Tersedia:"]
    for name, content in skills.items():
        desc = ""
        for line in content.split("\n"):
            if "description:" in line:
                desc = line.replace("description:", "").strip()
                break
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)

def install_skill(name, content):
    skill_dir = f"{SKILLS_DIR}/{name}"
    os.makedirs(skill_dir, exist_ok=True)
    with open(f"{skill_dir}/SKILL.md", "w") as f:
        f.write(content)
    if supabase:
        try:
            supabase.table("skills").upsert({"name": name, "content": content}).execute()
        except Exception:
            pass
    return f"✅ Skill '{name}' installed!"

def load_skills_from_supabase():
    if not supabase:
        return
    try:
        res = supabase.table("skills").select("*").execute()
        for row in (res.data or []):
            d = f"{SKILLS_DIR}/{row['name']}"
            os.makedirs(d, exist_ok=True)
            with open(f"{d}/SKILL.md", "w") as f:
                f.write(row["content"])
        if res.data:
            tulis_log(f"✅ {len(res.data)} skills dari Supabase")
    except Exception as e:
        tulis_log(f"Skills Supabase error: {e}")

# =================== TASKS ===================

def baca_tasks():
    with open(TASKS_FILE) as f:
        return json.load(f)

def simpan_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

def tambah_task(judul, deskripsi):
    tasks = baca_tasks()
    task = {"id": len(tasks)+1, "judul": judul, "deskripsi": deskripsi,
            "status": "pending", "dibuat": datetime.now().strftime("%Y-%m-%d %H:%M"), "selesai": None}
    tasks.append(task)
    simpan_tasks(tasks)
    if supabase:
        try:
            supabase.table("tasks").insert(task).execute()
        except Exception:
            pass
    return task

def update_task(task_id, status):
    tasks = baca_tasks()
    for t in tasks:
        if t["id"] == int(task_id):
            t["status"] = status
            if status == "selesai":
                t["selesai"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    simpan_tasks(tasks)

# =================== CONFIGS ===================

CONFIGURABLE_KEYS = {
    "GROQ_API_KEY":       {"label": "Groq API Key",       "url": "https://console.groq.com"},
    "GEMINI_API_KEY":     {"label": "Gemini API Key",     "url": "https://aistudio.google.com"},
    "OPENROUTER_API_KEY": {"label": "OpenRouter API Key", "url": "https://openrouter.ai"},
    "CEREBRAS_API_KEY":   {"label": "Cerebras API Key",   "url": "https://cloud.cerebras.ai"},
    "TOGETHER_API_KEY":   {"label": "Together AI Key",    "url": "https://api.together.ai"},
    "MISTRAL_API_KEY":    {"label": "Mistral API Key",    "url": "https://console.mistral.ai"},
    "BRAVE_API_KEY":      {"label": "Brave Search Key",   "url": "https://api.search.brave.com"},
    "GOOGLE_TOKEN":       {"label": "Google OAuth Token", "url": ""},
    "GITHUB_TOKEN":       {"label": "GitHub Token",       "url": "https://github.com/settings/tokens"},
    "YOUTUBE_API_KEY":    {"label": "YouTube API Key",    "url": "https://console.cloud.google.com"},
    "TELEGRAM_BOT_TOKEN": {"label": "Telegram Bot Token", "url": "https://t.me/BotFather"},
    "N8N_URL":            {"label": "N8N URL",            "url": "https://n8n.cloud"},
    "N8N_API_KEY":        {"label": "N8N API Key",        "url": "https://n8n.cloud"},
    "SUI_PRIVATE_KEY":    {"label": "SUI Wallet Key",     "url": "https://suiwallet.com"},
    "BINANCE_API_KEY":    {"label": "Binance API Key",    "url": "https://www.binance.com"},
}

def save_config(key, value):
    os.environ[key] = value
    if supabase:
        try:
            supabase.table("configs").upsert({"key": key, "value": value}).execute()
        except Exception:
            pass
    tulis_log(f"✅ Config '{key}' disimpan")

def load_configs():
    if not supabase:
        return
    try:
        res = supabase.table("configs").select("*").execute()
        for row in (res.data or []):
            os.environ[row["key"]] = row["value"]
        if res.data:
            tulis_log(f"✅ {len(res.data)} configs loaded")
    except Exception as e:
        tulis_log(f"Configs error: {e}")

def detect_api_key(text):
    text = text.strip()
    patterns = {
        "GROQ_API_KEY":       ["gsk_"],
        "GEMINI_API_KEY":     ["AIza"],
        "OPENROUTER_API_KEY": ["sk-or-"],
        "CEREBRAS_API_KEY":   ["csk-"],
        "GITHUB_TOKEN":       ["ghp_", "github_pat_"],
        "TELEGRAM_BOT_TOKEN": [":AAF", ":AAE", ":AAH"],
    }
    for key, prefixes in patterns.items():
        for prefix in prefixes:
            if text.lower().startswith(prefix.lower()):
                return key, text
    return None, None

# =================== BOTS ===================

_bots = {}

def load_bots():
    if supabase:
        try:
            res = supabase.table("bots").select("*").execute()
            for row in (res.data or []):
                _bots[row["slug"]] = row
            if res.data:
                tulis_log(f"✅ {len(res.data)} bots loaded")
        except Exception:
            pass

def save_bot(slug, name, description, system_prompt, warna="#0369a1"):
    slug = slug.lower().replace(" ", "-")
    html = f"""<!DOCTYPE html><html><head><title>{name}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Segoe UI',sans-serif;background:#020817;color:#e2e8f0;height:100vh;display:flex;justify-content:center;align-items:center}}.c{{width:96%;max-width:680px;height:95vh;background:#0a1628;border-radius:18px;display:flex;flex-direction:column;overflow:hidden;border:1px solid #1e3a5f}}.h{{background:{warna};padding:13px 18px;display:flex;align-items:center;gap:10px}}.h h1{{font-size:15px;font-weight:800;letter-spacing:2px}}.back{{margin-left:auto;font-size:10px;color:rgba(255,255,255,0.7);text-decoration:none;padding:4px 10px;border:1px solid rgba(255,255,255,0.3);border-radius:6px}}.msgs{{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px}}.msg{{padding:10px 14px;border-radius:12px;max-width:88%;word-wrap:break-word;white-space:pre-wrap;line-height:1.6;font-size:13px}}.user{{align-self:flex-end;background:#1e40af}}.bot{{align-self:flex-start;background:#071220;border:1px solid #1e3a5f}}.ia{{padding:10px;background:#020817;display:flex;gap:7px;border-top:1px solid #0f2744}}textarea{{flex:1;padding:10px;border-radius:9px;border:1px solid #0f2744;background:#0a1628;color:white;outline:none;font-size:13px;resize:none;height:42px;font-family:inherit}}button{{background:{warna};border:none;padding:0 16px;color:white;border-radius:9px;cursor:pointer;font-weight:700;font-size:13px}}</style></head>
<body><div class="c"><div class="h"><span style="font-size:20px">🤖</span><h1>{name.upper()}</h1><a href="/" class="back">← Openclaw</a></div>
<div id="cb" class="msgs"><div class="msg bot">👋 Halo! Aku {name}. {description}</div></div>
<div class="ia"><textarea id="inp" placeholder="Ketik pesan..." onkeypress="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();send()}}"></textarea><button id="btn" onclick="send()">Kirim</button></div></div>
<script>function add(c,t){{const cb=document.getElementById('cb');const d=document.createElement('div');d.className='msg '+c;d.textContent=t;cb.appendChild(d);cb.scrollTop=cb.scrollHeight;}}
async function send(){{const inp=document.getElementById('inp');const btn=document.getElementById('btn');const msg=inp.value.trim();if(!msg)return;add('user',msg);inp.value='';btn.disabled=true;
const th=document.createElement('div');th.className='msg bot';th.id='th';th.textContent='⏳...';document.getElementById('cb').appendChild(th);
try{{const r=await fetch('/bots/{slug}/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:msg}})}});const d=await r.json();document.getElementById('th')?.remove();add('bot',d.balasan);}}catch(e){{document.getElementById('th')?.remove();add('bot','❌ '+e.message);}}btn.disabled=false;inp.focus();}}</script></body></html>"""

    bot = {"slug": slug, "name": name, "description": description,
           "html": html, "system_prompt": system_prompt}
    _bots[slug] = bot
    if supabase:
        try:
            supabase.table("bots").upsert(bot).execute()
        except Exception:
            pass
    return f"✅ Bot '{name}' dibuat! Akses: /bots/{slug}"

# =================== CORE TOOLS ===================

def tool_web_search(query):
    brave = os.environ.get("BRAVE_API_KEY", "")
    if brave:
        try:
            r = requests.get(
                f"https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 5},
                headers={"Accept": "application/json", "X-Subscription-Token": brave},
                timeout=10
            )
            items = r.json().get("web", {}).get("results", [])
            return "\n\n".join([f"• {i['title']}\n  {i.get('description','')}\n  {i['url']}" for i in items[:5]])
        except Exception:
            pass
    try:
        r = requests.get(
            f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for t, s in zip(soup.select(".result__title")[:5], soup.select(".result__snippet")[:5]):
            results.append(f"• {t.get_text(strip=True)}: {s.get_text(strip=True)}")
        return "\n".join(results) or "Tidak ada hasil."
    except Exception as e:
        return f"Search gagal: {e}"

def tool_baca_url(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:5000]
    except Exception as e:
        return f"Gagal: {e}"

def tool_baca_file(path):
    try:
        full = f"{WORKSPACE}/{path}" if not path.startswith("/") else path
        with open(full) as f:
            return f.read()[:4000]
    except Exception as e:
        return f"Gagal: {e}"

def tool_tulis_file(path, content):
    try:
        full = f"{WORKSPACE}/{path}" if not path.startswith("/") else path
        os.makedirs(os.path.dirname(full) or WORKSPACE, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return f"✅ File disimpan: {path}"
    except Exception as e:
        return f"Gagal: {e}"

def tool_pip_install(package):
    """Install Python package"""
    try:
        import subprocess
        r = subprocess.run(
            ["pip", "install", package, "-q", "--break-system-packages"],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode == 0:
            return f"✅ {package} berhasil diinstall!"
        return f"❌ Gagal install {package}: {r.stderr[:300]}"
    except Exception as e:
        return f"❌ Error: {e}"

# =================== TOOLS DEFINITION ===================

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Cari informasi terbaru di internet.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "baca_url",
        "description": "Baca konten dari URL/website.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    }},
    {"type": "function", "function": {
        "name": "baca_file",
        "description": "Baca isi file dari workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "tulis_file",
        "description": "Tulis/simpan file ke workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "pip_install",
        "description": "Install Python package yang dibutuhkan MCP baru.",
        "parameters": {"type": "object", "properties": {"package": {"type": "string"}}, "required": ["package"]}
    }},
    {"type": "function", "function": {
        "name": "simpan_memory",
        "description": "Simpan info penting ke memory permanen.",
        "parameters": {"type": "object", "properties": {"info": {"type": "string"}}, "required": ["info"]}
    }},
    {"type": "function", "function": {
        "name": "lihat_memory",
        "description": "Lihat semua memory.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "buat_task",
        "description": "Buat task baru.",
        "parameters": {"type": "object", "properties": {
            "judul": {"type": "string"}, "deskripsi": {"type": "string"}
        }, "required": ["judul", "deskripsi"]}
    }},
    {"type": "function", "function": {
        "name": "lihat_tasks",
        "description": "Lihat semua task.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "selesaikan_task",
        "description": "Tandai task selesai.",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    }},
    {"type": "function", "function": {
        "name": "install_skill",
        "description": "Install skill baru dari konten SKILL.md.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "content": {"type": "string"}
        }, "required": ["name", "content"]}
    }},
    {"type": "function", "function": {
        "name": "lihat_skills",
        "description": "Lihat semua skill yang terinstall.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "install_mcp",
        "description": "Install MCP baru — simpan kode Python ke /tmp/openclaw/mcp/ dan Supabase. AI yang buat kodenya.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "nama mcp, contoh: mcp_youtube"},
            "code": {"type": "string", "description": "kode Python lengkap untuk MCP ini"},
            "description": {"type": "string"}
        }, "required": ["name", "code"]}
    }},
    {"type": "function", "function": {
        "name": "lihat_mcps",
        "description": "Lihat semua MCP yang terinstall.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "panggil_mcp",
        "description": "Panggil fungsi dari MCP yang sudah terinstall.",
        "parameters": {"type": "object", "properties": {
            "mcp_name": {"type": "string"},
            "func_name": {"type": "string"},
            "args": {"type": "object"}
        }, "required": ["mcp_name", "func_name"]}
    }},
    {"type": "function", "function": {
        "name": "cek_setup",
        "description": "Cek status semua API key.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "simpan_key",
        "description": "Simpan API key.",
        "parameters": {"type": "object", "properties": {
            "key_name": {"type": "string"}, "value": {"type": "string"}
        }, "required": ["key_name", "value"]}
    }},
    {"type": "function", "function": {
        "name": "buat_bot",
        "description": "Buat bot baru dengan dashboard sendiri.",
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string"}, "name": {"type": "string"},
            "description": {"type": "string"}, "system_prompt": {"type": "string"},
            "warna": {"type": "string", "default": "#0369a1"}
        }, "required": ["slug", "name", "description", "system_prompt"]}
    }},
    {"type": "function", "function": {
        "name": "lihat_bots",
        "description": "Lihat semua bot yang sudah dibuat.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "cek_provider",
        "description": "Cek status semua AI provider.",
        "parameters": {"type": "object", "properties": {}}
    }},
]

# =================== TOOL EXECUTOR ===================

def run_tool(name, args):
    if name == "web_search":       return tool_web_search(args.get("query",""))
    elif name == "baca_url":       return tool_baca_url(args.get("url",""))
    elif name == "baca_file":      return tool_baca_file(args.get("path",""))
    elif name == "tulis_file":     return tool_tulis_file(args.get("path",""), args.get("content",""))
    elif name == "pip_install":    return tool_pip_install(args.get("package",""))
    elif name == "simpan_memory":
        tulis_memory(args.get("info",""))
        return "✅ Memory disimpan"
    elif name == "lihat_memory":   return baca_memory()
    elif name == "buat_task":
        t = tambah_task(args.get("judul",""), args.get("deskripsi",""))
        return f"✅ Task #{t['id']} dibuat"
    elif name == "lihat_tasks":
        tasks = baca_tasks()
        if not tasks: return "Belum ada task."
        return "\n".join([f"{'✅' if t['status']=='selesai' else '⏳'} #{t['id']} {t['judul']} [{t['status']}]" for t in tasks])
    elif name == "selesaikan_task":
        update_task(args.get("task_id","0"), "selesai")
        return f"✅ Task selesai"
    elif name == "install_skill":  return install_skill(args.get("name",""), args.get("content",""))
    elif name == "lihat_skills":
        skills = load_skills()
        if not skills: return "Belum ada skill."
        return "\n".join([f"⚡ {n}" for n in skills.keys()])
    elif name == "install_mcp":
        ok, msg = install_mcp(args.get("name",""), args.get("code",""), args.get("description",""))
        return msg
    elif name == "lihat_mcps":
        mcps = list_mcps()
        return "\n".join([f"🔌 {m}" for m in mcps]) if mcps else "Belum ada MCP terinstall."
    elif name == "panggil_mcp":
        return call_mcp_function(args.get("mcp_name",""), args.get("func_name",""), args.get("args",{}))
    elif name == "cek_setup":
        ada = [f"✅ {v['label']}" for k,v in CONFIGURABLE_KEYS.items() if os.environ.get(k)]
        blm = [f"❌ {v['label']} — {v['url']}" for k,v in CONFIGURABLE_KEYS.items() if not os.environ.get(k)]
        return "📊 STATUS SETUP\n\n" + "\n".join(ada) + "\n\n❌ BELUM:\n" + "\n".join(blm[:5])
    elif name == "simpan_key":
        save_config(args.get("key_name",""), args.get("value",""))
        return f"✅ {args.get('key_name')} disimpan!"
    elif name == "buat_bot":
        return save_bot(args.get("slug",""), args.get("name",""), args.get("description",""),
                       args.get("system_prompt",""), args.get("warna","#0369a1"))
    elif name == "lihat_bots":
        return "\n".join([f"🤖 {b['name']} → /bots/{b['slug']}" for b in _bots.values()]) or "Belum ada bot."
    elif name == "cek_provider":
        providers = get_active_providers()
        return "\n".join([f"✅ {p['name']}" for p in providers]) or "❌ Tidak ada provider aktif."
    return f"❓ Tool '{name}' tidak dikenal."

# =================== AGENT ===================

_histories = {}

def run_agent(pesan, history=[]):
    memory    = baca_memory()[-1500:]
    skills    = skills_for_prompt()
    mcps_list = list_mcps()
    mcp_info  = f"\n\n## MCPs Terinstall: {', '.join(mcps_list)}" if mcps_list else "\n\n## MCPs: Belum ada. AI bisa install via perintah chat."
    pending   = [t for t in baca_tasks() if t["status"] == "pending"]
    task_info = f"\n\n## {len(pending)} Task Pending" if pending else ""
    mode      = needs_reasoning(pesan)

    # Reasoning pre-processing untuk pertanyaan kompleks
    reasoning_ctx = ""
    if mode:
        try:
            r_msgs = [
                {"role": "system", "content": "Kamu sistem reasoning internal. Analisis mendalam pertanyaan ini. Pertimbangkan semua sudut pandang, risiko, dan berikan rekomendasi konkret. Jawab dalam bahasa Indonesia."},
                {"role": "user", "content": pesan}
            ]
            r_data, r_prov = call_llm(r_msgs, reasoning=True, max_tokens=3000, temperature=0.6)
            raw = r_data["choices"][0]["message"].get("content", "")
            import re
            match = re.search(r'</think>(.*)', raw, re.DOTALL)
            reasoning_ctx = match.group(1).strip() if match else raw[:1500]
            tulis_log(f"🧠 Reasoning done [{r_prov}]")
        except Exception as e:
            tulis_log(f"Reasoning skip: {e}")

    system = f"""Kamu adalah Openclaw — autonomous AI agent yang cerdas dan proaktif.
## Cara Berpikir:
Sebelum bertindak, kamu selalu:
1. Pahami apa yang benar-benar dibutuhkan user
2. Analisis konteks dan informasi yang tersedia
3. Pertimbangkan opsi terbaik
4. Eksekusi dengan tools yang tepat
5. Verifikasi hasil dan laporkan dengan jelas
## Arsitektur:
- Server = manager ringan, hanya routing
- MCPs = modul terpisah di /tmp/openclaw/mcp/
- Skills = instruksi di /tmp/openclaw/skills/
- Semua tersimpan permanen di Supabase
## Kemampuan Kunci:
- Install MCP baru: cari cara kerjanya → tulis kode Python → install_mcp → install_skill
- Browser automation: panggil mcp_browser jika sudah terinstall
- Konek akun Google: pakai mcp_browser untuk OAuth flow
- Auto-install dependencies: pip_install sebelum install MCP baru
## Memory:
{memory}
{task_info}
{mcp_info}
{skills}
## Aturan:
1. Task baru → buat_task dulu
2. Info terbaru → web_search dulu  
3. Info penting → simpan_memory
4. MCP belum ada → install SEKALI saja, jangan loop
5. install_mcp HANYA dipanggil 1x per MCP — jangan panggil berulang
6. API key di-paste user → langsung simpan_key
7. JANGAN suruh buka Settings — semua lewat chat
8. LANGSUNG eksekusi, jangan banyak tanya
9. Jawab bahasa yang sama dengan user
10. Jika sudah install MCP → langsung test, jangan install ulang
## Mode: {"🧠 DEEP REASONING" if mode else "⚡ FAST"}"""

    messages = [{"role": "system", "content": system}]
    for h in history[-10:]:
        messages.append(h)

    if reasoning_ctx:
        user_content = f"""{pesan}
[Konteks reasoning]:
{reasoning_ctx}"""
    else:
        user_content = pesan

    messages.append({"role": "user", "content": user_content})

    tools_used = []
    for _ in range(6):  # max 6 tool calls per response
        try:
            data, provider = call_llm(messages, tools=TOOLS, max_tokens=2048)
            choice = data["choices"][0]
            msg    = choice["message"]
            reason = choice["finish_reason"]
            messages.append(msg)

            if reason == "tool_calls" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tname = tc["function"]["name"]
                    try:
                        targs = json.loads(tc["function"]["arguments"])
                    except Exception:
                        targs = {}
                    tools_used.append(tname)
                    tulis_log(f"Tool: {tname}")
                    hasil = run_tool(tname, targs)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(hasil)})
            else:
                balasan = msg.get("content", "...")
                new_history = history + [
                    {"role": "user", "content": pesan},
                    {"role": "assistant", "content": balasan}
                ]
                tulis_log(f"Chat [{provider}]: {pesan[:50]}")
                return balasan, tools_used, new_history, mode

        except Exception as e:
            tulis_log(f"Agent error: {e}")
            return f"❌ {str(e)}", tools_used, history, mode

    return "Selesai.", tools_used, history, mode

# =================== HTML ===================

HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Openclaw</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        *{box-sizing:border-box;margin:0;padding:0}
        body{font-family:'Segoe UI',sans-serif;background:#020817;color:#e2e8f0;height:100vh;display:flex;justify-content:center;align-items:center}
        .wrap{width:96%;max-width:640px;height:95vh;background:#0a1628;border-radius:18px;display:flex;flex-direction:column;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 60px rgba(14,165,233,0.08)}
        .head{background:linear-gradient(135deg,#0369a1,#1d4ed8);padding:12px 16px;display:flex;align-items:center;gap:10px}
        .head h1{font-size:15px;font-weight:800;letter-spacing:2px}
        .badges{margin-left:auto;display:flex;gap:6px}
        .badge{font-size:9px;padding:2px 8px;border-radius:10px;border:1px solid rgba(255,255,255,0.3);color:rgba(255,255,255,0.8)}
        .tabs{display:flex;background:#050d1a;border-bottom:1px solid #0f2744}
        .tab{flex:1;padding:8px;text-align:center;font-size:11px;cursor:pointer;color:#475569;font-weight:600;transition:all .2s}
        .tab.on{color:#38bdf8;border-bottom:2px solid #38bdf8;background:#071220}
        .panel{display:none;flex:1;overflow-y:auto;flex-direction:column}
        .panel.on{display:flex}
        .msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:7px}
        .msg{padding:10px 13px;border-radius:12px;max-width:90%;word-wrap:break-word;white-space:pre-wrap;line-height:1.6;font-size:13px;animation:fi .2s}
        @keyframes fi{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
        .user{align-self:flex-end;background:#1e40af;border-bottom-right-radius:3px}
        .bot{align-self:flex-start;background:#0f2032;border:1px solid #1e3a5f;border-bottom-left-radius:3px}
        .tbadge{align-self:flex-start;background:#071220;color:#0ea5e9;font-size:10px;padding:2px 9px;border-radius:20px;border:1px solid #0e3a5f}
        .rbadge{align-self:flex-start;background:#1e1040;color:#a78bfa;font-size:10px;padding:2px 9px;border-radius:20px;border:1px solid #4c1d95}
        .think{align-self:flex-start;color:#334155;font-size:11px;font-style:italic;padding:6px 12px}
        .inp-area{padding:10px;background:#020817;display:flex;gap:7px;border-top:1px solid #0f2744}
        textarea{flex:1;padding:10px;border-radius:9px;border:1px solid #0f2744;background:#0a1628;color:white;outline:none;font-size:13px;resize:none;height:42px;font-family:inherit}
        button{background:#1d4ed8;border:none;padding:0 15px;color:white;border-radius:9px;cursor:pointer;font-weight:700;font-size:13px}
        button:disabled{background:#1e293b;color:#475569;cursor:not-allowed}
        .txt-panel{flex:1;padding:12px;font-size:11px;color:#64748b;white-space:pre-wrap;font-family:monospace;line-height:1.7;overflow-y:auto}
        .card{background:#071220;border:1px solid #0f2744;border-radius:10px;padding:10px 12px;margin-bottom:8px}
        .card h3{font-size:12px;color:#38bdf8;margin-bottom:3px}
        .card p{font-size:11px;color:#64748b}
        .mcp-card{border-left:3px solid #10b981}
        .skill-card{border-left:3px solid #0ea5e9}
    </style>
</head>
<body>
<div class="wrap">
    <div class="head">
        <span style="font-size:20px">🦅</span>
        <h1>OPENCLAW</h1>
        <div class="badges">
            <span class="badge">AUTONOMOUS</span>
            <span class="badge" style="color:#10b981;border-color:#10b981">SUPABASE</span>
        </div>
    </div>
    <div class="tabs">
        <div class="tab on" onclick="tab('chat')">💬 Chat</div>
        <div class="tab" onclick="tab('mcp')">🔌 MCP</div>
        <div class="tab" onclick="tab('skills')">⚡ Skills</div>
        <div class="tab" onclick="tab('tasks')">✅ Tasks</div>
        <div class="tab" onclick="tab('memory')">🧠 Memory</div>
        <div class="tab" onclick="tab('bots')">🤖 Bots</div>
    </div>
    <div id="p-chat" class="panel on">
        <div id="cb" class="msgs">
            <div class="msg bot">🦅 Openclaw siap!
Perintah lewat chat:
• "konek Google" → login Google 1x
• "install MCP YouTube" → AI install sendiri
• "install MCP Gmail" → AI install sendiri
• "cek setup" → lihat status API key
• Paste API key disini → langsung tersimpan
AI yang urus semua — kamu cukup perintah! 🤖</div>
        </div>
        <div class="inp-area">
            <textarea id="inp" placeholder="Perintahkan sesuatu..." onkeypress="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"></textarea>
            <button id="btn" onclick="send()">Kirim</button>
        </div>
    </div>
    <div id="p-mcp" class="panel"><div id="mcp-content" class="txt-panel">Loading...</div></div>
    <div id="p-skills" class="panel"><div id="skills-content" class="txt-panel">Loading...</div></div>
    <div id="p-tasks" class="panel"><div id="tasks-content" class="txt-panel">Loading...</div></div>
    <div id="p-memory" class="panel"><div id="memory-content" class="txt-panel">Loading...</div></div>
    <div id="p-bots" class="panel"><div id="bots-content" class="txt-panel">Loading...</div></div>
</div>
<script>
function tab(t) {
    document.querySelectorAll('.tab').forEach((el,i)=>{
        el.classList.toggle('on',['chat','mcp','skills','tasks','memory','bots'][i]===t);
    });
    document.querySelectorAll('.panel').forEach(el=>el.classList.remove('on'));
    document.getElementById('p-'+t).classList.add('on');
    if(t==='mcp')     loadTxt('/mcps-list','mcp-content');
    if(t==='skills')  loadTxt('/skills','skills-content');
    if(t==='tasks')   loadTxt('/tasks-txt','tasks-content');
    if(t==='memory')  loadTxt('/memory','memory-content');
    if(t==='bots')    loadTxt('/bots-txt','bots-content');
}
async function loadTxt(url, id) {
    try {
        const r = await fetch(url);
        const d = await r.json();
        document.getElementById(id).textContent = d.content || '(kosong)';
    } catch(e) { document.getElementById(id).textContent = 'Error: '+e.message; }
}
function addMsg(cls, txt) {
    const cb = document.getElementById('cb');
    const d = document.createElement('div');
    d.className = 'msg '+cls;
    d.textContent = txt;
    cb.appendChild(d);
    cb.scrollTop = cb.scrollHeight;
}
async function send() {
    const inp = document.getElementById('inp');
    const btn = document.getElementById('btn');
    const msg = inp.value.trim();
    if(!msg) return;
    addMsg('user', msg);
    inp.value = '';
    btn.disabled = true;
    const thinking = document.createElement('div');
    thinking.className = 'think';
    thinking.id = 'thinking';
    const isDeep = msg.length > 100 || /analisis|strategi|kenapa|jelaskan|bagaimana|bandingkan|evaluasi|pikir|rekomendasi/i.test(msg);
    thinking.textContent = isDeep ? '🧠 Berpikir mendalam...' : '🦅 Mengerjakan...';
    document.getElementById('cb').appendChild(thinking);
    document.getElementById('cb').scrollTop = 99999;
    try {
        const r = await fetch('/chat', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({message: msg})
        });
        const d = await r.json();
        document.getElementById('thinking')?.remove();
        if(d.reasoning_used) {
            const rb = document.createElement('div');
            rb.className = 'rbadge';
            rb.textContent = '🧠 Deep Reasoning aktif';
            document.getElementById('cb').appendChild(rb);
        }
        if(d.tools_used && d.tools_used.length) {
            const tb = document.createElement('div');
            tb.className = 'tbadge';
            tb.textContent = '🔧 ' + d.tools_used.join(' → ');
            document.getElementById('cb').appendChild(tb);
        }
        addMsg('bot', d.balasan);
    } catch(e) {
        document.getElementById('thinking')?.remove();
        addMsg('bot', '❌ Error: '+e.message);
    }
    btn.disabled = false;
    inp.focus();
}
</script>
</body>
</html>"""

# =================== ROUTES ===================

@app.route('/')
def home():
    return render_template_string(HTML)

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        pesan = data.get("message","").strip()
        sid   = data.get("session_id","default")
        if not pesan:
            return jsonify({"balasan": "❌ Pesan kosong"}), 400

        # Auto-detect API key
        key_name, key_value = detect_api_key(pesan)
        if key_name and key_value:
            save_config(key_name, key_value)
            return jsonify({
                "balasan": f"✅ {key_name} tersimpan dan aktif!\n\nKetik 'cek provider' untuk lihat statusnya.",
                "tools_used": ["simpan_key"], "reasoning_used": False
            })

        history = _histories.get(sid, [])
        balasan, tools, new_history, reasoning = run_agent(pesan, history)
        _histories[sid] = new_history[-16:]

        if supabase:
            try:
                supabase.table("chat_logs").insert({
                    "user_msg": pesan, "bot_reply": balasan,
                    "tools_used": ",".join(tools) if tools else None,
                    "reasoning_used": reasoning
                }).execute()
            except Exception:
                pass

        return jsonify({"balasan": balasan, "tools_used": tools, "reasoning_used": reasoning})

    except Exception as e:
        tulis_log(f"Chat error: {e}")
        return jsonify({"balasan": f"❌ {str(e)}"}), 500

@app.route('/memory')
def get_memory():
    return jsonify({"content": baca_memory()})

@app.route('/tasks-txt')
def get_tasks_txt():
    tasks = baca_tasks()
    if not tasks:
        return jsonify({"content": "Belum ada task."})
    lines = []
    for t in reversed(tasks):
        icon = "✅" if t["status"] == "selesai" else "⏳"
        lines.append(f"{icon} #{t['id']} [{t['status']}] {t['judul']}\n   {t['deskripsi']}\n   {t['dibuat']}")
    return jsonify({"content": "\n\n".join(lines)})

@app.route('/skills')
def get_skills():
    skills = load_skills()
    if not skills:
        return jsonify({"content": "Belum ada skill.\n\nKetik: 'install MCP Browser' untuk mulai!"})
    lines = [f"📦 {len(skills)} Skills terinstall:\n"]
    for name, content in skills.items():
        desc = ""
        for line in content.split("\n"):
            if "description:" in line:
                desc = line.replace("description:","").strip()
                break
        lines.append(f"⚡ {name}\n   {desc}")
    return jsonify({"content": "\n\n".join(lines)})

@app.route('/mcps-list')
def get_mcps():
    mcps = list_mcps()
    if not mcps:
        return jsonify({"content": "Belum ada MCP terinstall.\n\nContoh:\n• 'install MCP Browser'\n• 'install MCP YouTube'\n• 'install MCP Gmail'\n\nAI yang install semuanya!"})
    lines = [f"🔌 {len(mcps)} MCP terinstall:\n"]
    for name in mcps:
        lines.append(f"• {name}")
    return jsonify({"content": "\n".join(lines)})

@app.route('/bots-txt')
def get_bots_txt():
    if not _bots:
        return jsonify({"content": "Belum ada bot.\n\nContoh: 'buatkan bot customer service untuk tokoku'"})
    lines = [f"🤖 {len(_bots)} Bot:\n"]
    for b in _bots.values():
        lines.append(f"• {b['name']} → /bots/{b['slug']}\n  {b['description']}")
    return jsonify({"content": "\n\n".join(lines)})

@app.route('/bots/<slug>')
def bot_page(slug):
    bot = _bots.get(slug)
    if not bot:
        return f"<h2 style='color:white;font-family:sans-serif;padding:40px'>Bot '{slug}' tidak ditemukan. <a href='/' style='color:#38bdf8'>← Kembali</a></h2>", 404
    return bot["html"]

@app.route('/bots/<slug>/chat', methods=['POST'])
def bot_chat(slug):
    bot = _bots.get(slug)
    if not bot:
        return jsonify({"balasan": f"❌ Bot '{slug}' tidak ditemukan"}), 404
    try:
        data  = request.get_json()
        pesan = data.get("message","").strip()
        sid   = data.get("session_id", f"bot_{slug}")
        if not pesan:
            return jsonify({"balasan": "❌ Kosong"}), 400

        history  = _histories.get(sid, [])
        messages = [{"role": "system", "content": bot["system_prompt"]}]
        for h in history[-8:]:
            messages.append(h)
        messages.append({"role": "user", "content": pesan})

        tools_used = []
        for _ in range(8):
            data_r, provider = call_llm(messages, tools=TOOLS, max_tokens=1024)
            choice = data_r["choices"][0]
            msg    = choice["message"]
            messages.append(msg)
            if choice["finish_reason"] == "tool_calls" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tname = tc["function"]["name"]
                    try: targs = json.loads(tc["function"]["arguments"])
                    except: targs = {}
                    tools_used.append(tname)
                    hasil = run_tool(tname, targs)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(hasil)})
            else:
                balasan = msg.get("content","...")
                _histories[sid] = (history + [
                    {"role":"user","content":pesan},
                    {"role":"assistant","content":balasan}
                ])[-12:]
                return jsonify({"balasan": balasan, "tools_used": tools_used})

        return jsonify({"balasan": "Selesai.", "tools_used": tools_used})
    except Exception as e:
        return jsonify({"balasan": f"❌ {str(e)}"}), 500

# =================== STARTUP ===================

if __name__ == "__main__":
    print("🦅 Openclaw starting...")
    load_configs()
    load_all_mcps()
    load_skills_from_supabase()
    load_bots()
    print("✅ Openclaw ready!")
    app.run(host="0.0.0.0", port=7860, debug=False)
