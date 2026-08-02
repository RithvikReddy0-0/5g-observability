from flask import Flask, request, Response
from datetime import datetime
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)

# 🔥 Prometheus Metric
REQUEST_COUNT = Counter(
    'traffic_requests_total',
    'Total traffic requests',
    ['slice_type']
)

# 📄 Log file
LOG_FILE = "server_logs.txt"

# 🎯 Slice Configuration
SLICES = {
    "video": {"priority": 1, "bandwidth": "high"},
    "file": {"priority": 2, "bandwidth": "medium"},
    "web": {"priority": 3, "bandwidth": "low"}
}

# 📝 Logging function
def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{now}] SERVER | {message}"

    print(log_msg)

    with open(LOG_FILE, "a") as f:
        f.write(log_msg + "\n")

# 🔥 Core slicing handler (INSTRUMENTED)
def handle_slice(traffic_type, size=0):
    slice_info = SLICES[traffic_type]

    # 🚀 PROMETHEUS METRIC (CRITICAL)
    REQUEST_COUNT.labels(slice_type=traffic_type).inc()

    log(
        f"{traffic_type.upper()} → Slice Assigned | "
        f"Priority: {slice_info['priority']} | "
        f"Bandwidth: {slice_info['bandwidth']} | "
        f"Size: {size}"
    )

    return slice_info

# 📹 Video Traffic
@app.route("/video", methods=["POST"])
def video():
    size = request.form.get("size", 0)
    handle_slice("video", size)
    return "ok"

# 📂 File Traffic
@app.route("/file", methods=["POST"])
def file():
    size = request.form.get("size", 0)
    handle_slice("file", size)
    return "ok"

# 🌐 Web Traffic
@app.route("/web", methods=["GET"])
def web():
    handle_slice("web")
    return "ok"

# 📊 Prometheus Metrics Endpoint (CRITICAL)
@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

# 🚀 Start server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
