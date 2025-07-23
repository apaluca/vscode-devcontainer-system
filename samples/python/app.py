#!/usr/bin/env python3
"""
Sample Python application demonstrating DevContainer support
"""

from flask import Flask, jsonify
from datetime import datetime
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        'message': 'Hello from Python DevContainer!',
        'timestamp': datetime.now().isoformat(),
        'python_version': os.popen('python --version').read().strip(),
        'environment': 'development'
    })

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
