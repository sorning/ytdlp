from flask import Flask, request, jsonify, send_from_directory
from yt_dlp import YoutubeDL
from celery import Celery
import os
import glob
import uuid
import traceback
import shutil
from datetime import datetime, timedelta

# 创建 Flask 应用
app = Flask(__name__)

# 配置 Celery（注意 Redis 地址）
def make_celery(app):
    celery = Celery(app.import_name, backend='redis://redis:6379/0', broker='redis://redis:6379/0')
    celery.conf.update(app.config)
    return celery

celery = make_celery(app)

# 全局下载目录
BASE_DOWNLOAD_DIR = "/downloads"
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

# 每个下载任务对应一个唯一目录
@celery.task(bind=True)
def download_video_task(self, url):
    task_id = str(uuid.uuid4())
    task_download_dir = os.path.join(BASE_DOWNLOAD_DIR, task_id)
    os.makedirs(task_download_dir, exist_ok=True)

    ydl_opts = {
        'format': 'bestvideo+bestaudio',
        'outtmpl': f'{task_download_dir}/%(title).50s.%(ext)s',
        'merge_output_format': 'mp4',
        'ffmpeg_location': '/usr/bin/ffmpeg',
        'restrictfilenames': True,
        'postprocessor_args': [
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k',
            '-movflags', '+faststart'
        ],
        'quiet': False,
        'nopart': True,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([url])
            if result != 0:
                raise Exception(f"yt-dlp failed with code {result}")

        mp4_files = glob.glob(f"{task_download_dir}/*.mp4")
        if not mp4_files:
            raise Exception("No .mp4 file found after download")

        latest_file = max(mp4_files, key=os.path.getctime)

        return {
            "message": "Download completed",
            "filename": os.path.basename(latest_file),
            "download_url": f"/files/{task_id}/{os.path.basename(latest_file)}"
        }

    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error occurred: {traceback_str}")
        raise self.retry(exc=e)

@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json()
    url = data.get("url")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    task = download_video_task.apply_async(args=[url])
    return jsonify({"task_id": task.id}), 202

@app.route('/task_status/<task_id>')
def task_status(task_id):
    task = download_video_task.AsyncResult(task_id)

    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Task is still in progress.'
        }
    elif task.state == 'SUCCESS':
        response = {
            'state': task.state,
            'result': task.result
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'error': str(task.info)
        }
    else:
        response = {
            'state': task.state,
            'status': 'Task failed.'
        }

    return jsonify(response)

@app.route('/files/<task_id>/<filename>')
def get_file(task_id, filename):
    dir_path = os.path.join(BASE_DOWNLOAD_DIR, task_id)
    return send_from_directory(dir_path, filename, as_attachment=True)

# ✅ 定时清理函数：清除超过 1 小时的任务文件夹
@celery.task
def cleanup_old_tasks(hours=1):
    now = datetime.now()
    cutoff = now - timedelta(hours=hours)

    for dir_name in os.listdir(BASE_DOWNLOAD_DIR):
        dir_path = os.path.join(BASE_DOWNLOAD_DIR, dir_name)
        if os.path.isdir(dir_path):
            created_time = datetime.fromtimestamp(os.path.getctime(dir_path))
            if created_time < cutoff:
                print(f"Removing old directory: {dir_path}")
                shutil.rmtree(dir_path)

# 示例手动触发清理路由（你也可以用 cron + curl 来触发它）
@app.route('/cleanup', methods=['POST'])
def trigger_cleanup():
    cleanup_old_tasks.delay()
    return jsonify({"message": "Cleanup task started."})

# 首页 HTML 不变（省略）

@app.route('/')
def home():
    return """
    <!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YT-DLP Downloader</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            background-color: #f4f4f4;
        }
        h1 {
            color: #333;
        }
        form {
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
            width: 90%;
            max-width: 400px;
        }
        input {
            width: calc(100% - 22px);
            padding: 10px;
            margin-bottom: 10px;
            border: 1px solid #ccc;
            border-radius: 5px;
            font-size: 16px;
        }
        button {
            width: 100%;
            padding: 10px;
            background-color: #007BFF;
            color: white;
            border: none;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
        }
        button:hover {
            background-color: #0056b3;
        }
        #downloadLink {
            display: none;
            margin-top: 20px;
        }
    </style>
    <script>
        async function submitForm() {
            let url = document.getElementById("url").value;
            if (!url) {
                alert("Please enter a valid URL");
                return;
            }

            try {
                const response = await fetch("/download", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ url: url })
                });

                if (!response.ok) {
                    throw new Error("Network response was not ok");
                }

                const data = await response.json();
                const taskId = data.task_id;

                console.log("Task ID:", taskId); // 调试输出

                let statusData = { state: "PENDING" };
                // 轮询任务状态直到完成
                while (statusData.state === "PENDING" || statusData.state === "STARTED") {
                    console.log("Checking task status..."); // 调试输出
                    await new Promise(resolve => setTimeout(resolve, 2000)); // 等待2秒
                    const statusResponse = await fetch(`/task_status/${taskId}`);
                    statusData = await statusResponse.json();
                    console.log("Task Status:", statusData); // 调试输出
                }



                if (statusData.state === 'SUCCESS') {
                    let link = document.getElementById("downloadLink");
                    link.href = statusData.result.download_url;
                    link.innerText = "Download " + statusData.result.filename;
                    link.style.display = "block";
                } else {
                    alert("Error: " + (statusData.error || "Unknown error"));
                }
            } catch (error) {
                console.error("Error:", error);
                alert("Failed to download");
            }
        }
    </script>
</head>
<body>
    <h1>YT-DLP Flask Server</h1>
    <form onsubmit="event.preventDefault(); submitForm();">
        <input type="text" id="url" name="url" placeholder="Enter YouTube Shorts URL">
        <button type="submit">Download</button>
    </form>
    <a id="downloadLink" href="#" download></a>
</body>
</html>
    """

if __name__ == '__main__':
    app.run(debug=True)
