#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube 高画質ダウンローダー - WebUI版(単一ファイル・完全ローカル動作)

実行するとローカルホスト(http://127.0.0.1:5000)にサーバーが立ち上がり、
自動的にブラウザが開きます。ダウンロードしたファイルはサーバーを実行している
マシン(=あなたのPC)上に直接保存されます。外部にアップロードされることは
ありません。

必要なもの:
    pip install -U yt-dlp flask
    ffmpeg (映像+音声の結合・デインターレースに必要。PATHが通っている必要があります)
        Windows: https://www.gyan.dev/ffmpeg/builds/ からダウンロードしPATHに追加
        Mac:     brew install ffmpeg
        Linux:   sudo apt install ffmpeg  など

起動方法:
    python youtube_downloader_webui.py
    (自動でブラウザが開きます。開かない場合は http://127.0.0.1:5000 にアクセス)

403エラーについて:
    複数のプレイヤークライアント切り替え・UA偽装・リトライ強化で軽減しますが、
    完全には防げません。頻発する場合は `pip install -U yt-dlp` で更新してください。
"""

import os
import sys
import uuid
import shutil
import threading
import subprocess
import webbrowser
from datetime import datetime

try:
    from flask import Flask, request, jsonify, Response
except ImportError:
    print("Flask がインストールされていません。")
    print("次のコマンドでインストールしてください: pip install -U flask")
    sys.exit(1)

try:
    import yt_dlp
except ImportError:
    print("yt-dlp がインストールされていません。")
    print("次のコマンドでインストールしてください: pip install -U yt-dlp")
    sys.exit(1)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_SAVE_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
HOST = "127.0.0.1"
PORT = 5000

app = Flask(__name__)

# job_id -> ジョブの状態を保持する辞書(スレッド間で共有)
JOBS = {}
JOBS_LOCK = threading.Lock()


def check_ffmpeg() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def new_job():
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",       # queued / downloading / postprocessing / deinterlacing / done / error
            "percent": 0.0,
            "speed": "",
            "logs": [],
            "error": None,
            "filepath": None,
        }
    return job_id


def log(job_id: str, message: str):
    ts = datetime.now().strftime("%H:%M:%S")
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["logs"].append(f"[{ts}] {message}")


def set_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def deinterlace_file(path: str, job_id: str) -> str:
    """ffmpegのyadifフィルタでインターレース解除を行い、ファイルを置き換える"""
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"デインターレース対象が見つかりません: {path}")

    root, ext = os.path.splitext(path)
    tmp_path = f"{root}.deinterlaced{ext}"

    set_job(job_id, status="deinterlacing")
    log(job_id, "インターレース解除中(ffmpeg / yadif)...")

    cmd = [
        "ffmpeg", "-y",
        "-i", path,
        "-vf", "yadif=1",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-c:a", "copy",
        tmp_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0 or not os.path.exists(tmp_path):
        err = result.stderr.decode("utf-8", errors="ignore")[-800:]
        raise RuntimeError(f"デインターレース処理に失敗しました:\n{err}")

    os.remove(path)
    shutil.move(tmp_path, path)
    log(job_id, "インターレース解除が完了しました")
    return path


def run_download(job_id: str, url: str, quality: str, want_deinterlace: bool, save_dir: str):
    try:
        os.makedirs(save_dir, exist_ok=True)
        set_job(job_id, status="downloading")
        log(job_id, f"ダウンロード開始: {url}")
        log(job_id, f"画質設定: {quality} / 保存先: {save_dir}")

        if quality == "audio":
            fmt = "bestaudio/best"
            postprocessors = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
            ]
            merge_ext = None
        else:
            if quality == "best":
                fmt = "bestvideo+bestaudio/best"
            else:
                fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
            postprocessors = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
            merge_ext = "mp4"

        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                percent = (downloaded / total * 100) if total else 0
                set_job(job_id, percent=percent, speed=d.get("_speed_str", "").strip())
            elif d["status"] == "finished":
                set_job(job_id, status="postprocessing", percent=100)
                log(job_id, "ダウンロード完了、結合処理中(ffmpeg)...")

        ydl_opts = {
            "format": fmt,
            "outtmpl": os.path.join(save_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [hook],
            "postprocessors": postprocessors,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            # --- 403対策 ---
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
            "geo_bypass": True,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "extractor_retries": 5,
            "socket_timeout": 30,
        }
        if merge_ext:
            ydl_opts["merge_output_format"] = merge_ext

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        final_path = None
        if info:
            reqs = info.get("requested_downloads")
            if reqs:
                final_path = reqs[0].get("filepath") or reqs[0].get("_filename")
            if not final_path:
                final_path = ydl.prepare_filename(info)
                if merge_ext:
                    base, _ = os.path.splitext(final_path)
                    candidate = f"{base}.{merge_ext}"
                    if os.path.exists(candidate):
                        final_path = candidate

        if want_deinterlace and quality != "audio" and final_path:
            deinterlace_file(final_path, job_id)

        set_job(job_id, status="done", percent=100, filepath=final_path)
        log(job_id, f"完了: {final_path}")

    except Exception as e:
        msg = str(e)
        set_job(job_id, status="error", error=msg)
        hint = ""
        if "403" in msg:
            hint = " / 403エラー: pip install -U yt-dlp で更新後に再試行してください"
        log(job_id, f"エラー: {msg}{hint}")


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")
    deinterlace = bool(data.get("deinterlace", False))
    save_dir = (data.get("save_dir") or DEFAULT_SAVE_DIR).strip()

    if not url:
        return jsonify({"error": "URLを入力してください"}), 400

    job_id = new_job()
    t = threading.Thread(
        target=run_download,
        args=(job_id, url, quality, deinterlace, save_dir),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "ジョブが見つかりません"}), 404
        return jsonify(dict(job))


@app.route("/api/ffmpeg_check", methods=["GET"])
def api_ffmpeg_check():
    return jsonify({"ok": check_ffmpeg()})


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# フロントエンド(単一ファイルに埋め込み)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YT Local Downloader</title>
<style>
  :root{
    --bg:#0B0F14;
    --panel:#121822;
    --panel2:#161E2B;
    --border:#232C3B;
    --text:#E7ECF3;
    --muted:#7C8798;
    --accent:#3FD6C6;
    --accent-dim:#1F5F58;
    --warn:#F5A623;
    --err:#F0556B;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;}
  body{
    background:
      radial-gradient(circle at 15% 0%, rgba(63,214,198,0.06), transparent 40%),
      var(--bg);
    color:var(--text);
    font-family:'Inter','Hiragino Kaku Gothic ProN','Yu Gothic',sans-serif;
    min-height:100vh;
    padding:32px 20px 60px;
  }
  .wrap{max-width:960px;margin:0 auto;}
  header{
    display:flex;align-items:baseline;justify-content:space-between;
    margin-bottom:28px;flex-wrap:wrap;gap:10px;
  }
  .brand{display:flex;align-items:center;gap:12px;}
  .brand-mark{
    width:34px;height:34px;border-radius:8px;
    background:linear-gradient(135deg,var(--accent),var(--accent-dim));
    display:flex;align-items:center;justify-content:center;
    font-family:'JetBrains Mono',monospace;font-weight:700;color:#04120F;font-size:14px;
  }
  h1{
    font-family:'Space Grotesk','Inter',sans-serif;
    font-size:20px;font-weight:600;letter-spacing:0.02em;margin:0;
  }
  .sub{color:var(--muted);font-size:12.5px;font-family:'JetBrains Mono',monospace;}
  .ffmpeg-pill{
    font-family:'JetBrains Mono',monospace;font-size:11.5px;
    padding:5px 10px;border-radius:100px;border:1px solid var(--border);
    color:var(--muted);
  }
  .ffmpeg-pill.ok{color:var(--accent);border-color:var(--accent-dim);}
  .ffmpeg-pill.bad{color:var(--err);border-color:#4a1f27;}

  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
  @media(max-width:820px){.grid{grid-template-columns:1fr;}}

  .panel{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:14px;
    padding:20px;
  }
  .panel h2{
    font-family:'JetBrains Mono',monospace;
    font-size:11px;letter-spacing:0.14em;text-transform:uppercase;
    color:var(--muted);margin:0 0 16px;
    display:flex;align-items:center;gap:8px;
  }
  .panel h2::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--accent);}

  label{display:block;font-size:12.5px;color:var(--muted);margin:14px 0 6px;}
  label:first-child{margin-top:0;}
  input[type=text]{
    width:100%;background:var(--panel2);border:1px solid var(--border);
    color:var(--text);border-radius:8px;padding:10px 12px;font-size:14px;
    font-family:inherit;
  }
  input[type=text]:focus{outline:none;border-color:var(--accent-dim);}

  .radio-row{display:flex;flex-direction:column;gap:6px;}
  .radio-item{
    display:flex;align-items:center;gap:9px;
    background:var(--panel2);border:1px solid var(--border);
    border-radius:8px;padding:9px 12px;cursor:pointer;font-size:13.5px;
    transition:border-color .12s ease;
  }
  .radio-item:hover{border-color:var(--accent-dim);}
  .radio-item input{accent-color:var(--accent);}
  .radio-item.checked{border-color:var(--accent);background:#12241F;}

  .check-item{
    display:flex;align-items:center;gap:9px;
    background:var(--panel2);border:1px solid var(--border);
    border-radius:8px;padding:9px 12px;font-size:13px;color:var(--text);margin-top:14px;
  }
  .check-item input{accent-color:var(--accent);}
  .hint{color:var(--muted);font-size:11px;margin-top:4px;}

  .dir-row{display:flex;gap:8px;}
  .dir-row input{flex:1;}
  .dir-row button{
    background:var(--panel2);border:1px solid var(--border);color:var(--muted);
    border-radius:8px;padding:0 12px;font-size:12px;cursor:pointer;font-family:inherit;
  }

  .go-btn{
    margin-top:18px;width:100%;padding:13px;border:none;border-radius:10px;
    background:var(--accent);color:#04120F;font-weight:700;font-size:14.5px;
    cursor:pointer;font-family:'Space Grotesk',inherit;letter-spacing:0.01em;
    transition:filter .12s ease, transform .05s ease;
  }
  .go-btn:hover{filter:brightness(1.08);}
  .go-btn:active{transform:scale(0.99);}
  .go-btn:disabled{background:var(--panel2);color:var(--muted);cursor:not-allowed;}

  /* --- 右側:信号ログパネル(シグネチャ要素) --- */
  .scope{
    position:relative;height:64px;border:1px solid var(--border);border-radius:8px;
    background:
      repeating-linear-gradient(0deg, rgba(63,214,198,0.05) 0px, rgba(63,214,198,0.05) 1px, transparent 1px, transparent 8px),
      var(--panel2);
    overflow:hidden;margin-bottom:14px;
  }
  .scope .line{
    position:absolute;left:0;top:50%;width:100%;height:1px;background:var(--border);
  }
  .scope .sweep{
    position:absolute;top:0;left:-20%;width:20%;height:100%;
    background:linear-gradient(90deg, transparent, rgba(63,214,198,0.35), transparent);
    animation:sweep 2.1s linear infinite;
    display:none;
  }
  .scope.active .sweep{display:block;}
  @keyframes sweep{
    0%{left:-20%;}
    100%{left:100%;}
  }
  .scope .pct{
    position:absolute;right:10px;bottom:6px;font-family:'JetBrains Mono',monospace;
    font-size:20px;color:var(--accent);
  }
  .scope .label{
    position:absolute;left:10px;top:6px;font-family:'JetBrains Mono',monospace;
    font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);
  }

  .status-line{
    font-family:'JetBrains Mono',monospace;font-size:12.5px;color:var(--text);
    margin-bottom:10px;display:flex;justify-content:space-between;
  }
  .status-line .speed{color:var(--muted);}

  .log{
    background:#070A0E;border:1px solid var(--border);border-radius:8px;
    height:280px;overflow-y:auto;padding:12px;
    font-family:'JetBrains Mono',monospace;font-size:11.5px;line-height:1.7;
    color:#9FB0C3;
  }
  .log .err{color:var(--err);}
  .log .ok{color:var(--accent);}
  .log::-webkit-scrollbar{width:8px;}
  .log::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px;}

  footer{
    text-align:center;color:var(--muted);font-size:11px;margin-top:26px;
    font-family:'JetBrains Mono',monospace;
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="brand-mark">YT</div>
      <div>
        <h1>Local Downloader</h1>
        <div class="sub">127.0.0.1:5000 — 完全ローカル動作</div>
      </div>
    </div>
    <div id="ffmpegPill" class="ffmpeg-pill">ffmpeg 確認中...</div>
  </header>

  <div class="grid">
    <!-- 左: 操作パネル -->
    <div class="panel">
      <h2>Source</h2>
      <label>動画URL</label>
      <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=...">

      <label>画質</label>
      <div class="radio-row" id="qualityRow">
        <label class="radio-item checked"><input type="radio" name="quality" value="best" checked>最高画質(推奨・4K/8K対応)</label>
        <label class="radio-item"><input type="radio" name="quality" value="1080">1080p以下</label>
        <label class="radio-item"><input type="radio" name="quality" value="720">720p以下</label>
        <label class="radio-item"><input type="radio" name="quality" value="480">480p以下</label>
        <label class="radio-item"><input type="radio" name="quality" value="audio">音声のみ(MP3)</label>
      </div>

      <div class="check-item">
        <input type="checkbox" id="deinterlace">
        <span>インターレース解除(yadif) — 処理に時間がかかります</span>
      </div>

      <label>保存先フォルダ(サーバー側のパス)</label>
      <div class="dir-row">
        <input type="text" id="saveDir" value="__DEFAULT_DIR__">
      </div>
      <div class="hint">このアプリを実行しているPC上のフォルダに直接保存されます。</div>

      <button class="go-btn" id="goBtn" onclick="startDownload()">ダウンロード開始</button>
    </div>

    <!-- 右: 信号ログパネル -->
    <div class="panel">
      <h2>Signal</h2>
      <div class="scope" id="scope">
        <div class="label">progress</div>
        <div class="line"></div>
        <div class="sweep"></div>
        <div class="pct" id="pct">0%</div>
      </div>
      <div class="status-line">
        <span id="statusText">待機中</span>
        <span class="speed" id="speedText"></span>
      </div>
      <div class="log" id="log">ジョブを開始するとここにログが表示されます。</div>
    </div>
  </div>

  <footer>すべての処理はこの端末内で完結します。外部への動画アップロードは行いません。</footer>
</div>

<script>
let polling = null;

document.querySelectorAll('input[name=quality]').forEach(r => {
  r.addEventListener('change', () => {
    document.querySelectorAll('.radio-item').forEach(el => el.classList.remove('checked'));
    r.closest('.radio-item').classList.add('checked');
  });
});

fetch('/api/ffmpeg_check').then(r => r.json()).then(d => {
  const pill = document.getElementById('ffmpegPill');
  if (d.ok) { pill.textContent = 'ffmpeg OK'; pill.classList.add('ok'); }
  else { pill.textContent = 'ffmpeg 未検出'; pill.classList.add('bad'); }
});

function appendLog(line, cls) {
  const log = document.getElementById('log');
  if (log.textContent.startsWith('ジョブを開始すると')) log.textContent = '';
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = line;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function startDownload() {
  const url = document.getElementById('url').value.trim();
  if (!url) { alert('URLを入力してください'); return; }
  const quality = document.querySelector('input[name=quality]:checked').value;
  const deinterlace = document.getElementById('deinterlace').checked;
  const save_dir = document.getElementById('saveDir').value.trim();

  const btn = document.getElementById('goBtn');
  btn.disabled = true;
  btn.textContent = '実行中...';
  document.getElementById('log').textContent = '';
  document.getElementById('scope').classList.add('active');

  fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, quality, deinterlace, save_dir})
  }).then(r => r.json()).then(d => {
    if (d.error) { appendLog('エラー: ' + d.error, 'err'); resetBtn(); return; }
    poll(d.job_id);
  }).catch(e => { appendLog('通信エラー: ' + e, 'err'); resetBtn(); });
}

function resetBtn() {
  const btn = document.getElementById('goBtn');
  btn.disabled = false;
  btn.textContent = 'ダウンロード開始';
  document.getElementById('scope').classList.remove('active');
}

let lastLogCount = 0;

function poll(jobId) {
  lastLogCount = 0;
  polling = setInterval(() => {
    fetch('/api/status/' + jobId).then(r => r.json()).then(d => {
      if (d.error && !d.status) { appendLog('エラー: ' + d.error, 'err'); clearInterval(polling); resetBtn(); return; }

      const pct = Math.round(d.percent || 0);
      document.getElementById('pct').textContent = pct + '%';
      document.getElementById('speedText').textContent = d.speed || '';

      const statusMap = {
        queued: '待機中...',
        downloading: 'ダウンロード中...',
        postprocessing: '結合処理中(ffmpeg)...',
        deinterlacing: 'インターレース解除中...',
        done: '完了しました',
        error: 'エラーが発生しました'
      };
      document.getElementById('statusText').textContent = statusMap[d.status] || d.status;

      (d.logs || []).slice(lastLogCount).forEach(line => {
        appendLog(line, line.includes('エラー') ? 'err' : (line.includes('完了') ? 'ok' : ''));
      });
      lastLogCount = (d.logs || []).length;

      if (d.status === 'done') {
        clearInterval(polling);
        resetBtn();
        document.getElementById('scope').classList.remove('active');
      } else if (d.status === 'error') {
        clearInterval(polling);
        resetBtn();
        document.getElementById('scope').classList.remove('active');
      }
    }).catch(() => {});
  }, 800);
}
</script>
</body>
</html>
"""

INDEX_HTML = INDEX_HTML.replace("__DEFAULT_DIR__", DEFAULT_SAVE_DIR.replace("\\", "\\\\"))


def open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


def main():
    if not check_ffmpeg():
        print("警告: ffmpeg が見つかりません。結合・デインターレースが失敗します。")
        print("Windows: https://www.gyan.dev/ffmpeg/builds/ / Mac: brew install ffmpeg / Linux: sudo apt install ffmpeg")

    print(f"サーバーを起動します: http://{HOST}:{PORT}")
    threading.Timer(1.0, open_browser).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
