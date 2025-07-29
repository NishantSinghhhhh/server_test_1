
import os
import json
import time
import threading
from datetime import datetime
from pathlib import Path

# Basic imports
from flask import Flask, jsonify
from flask_cors import CORS
import websockets
import asyncio
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# OCR Libraries
try:
    import pytesseract
    from PIL import Image
    TESSERACT_OK = True
    print("✅ Tesseract available")
except ImportError:
    TESSERACT_OK = False
    print("❌ Tesseract not available")

try:
    import easyocr
    EASYOCR_OK = True
    print("✅ EasyOCR available")
except ImportError:
    EASYOCR_OK = False
    print("❌ EasyOCR not available")

# Configuration
WATCH_DIR = "/home/nishant/Pictures/Screenshots"
HTTP_PORT = 3001
WS_PORT = 3002

# Global variables
websocket_clients = set()
is_processing = False
processed_files = set()
ocr_reader = None
websocket_server = None

def init_easyocr():
    """Initialize EasyOCR"""
    global ocr_reader
    if EASYOCR_OK and ocr_reader is None:
        try:
            print("🔧 Loading EasyOCR...")
            ocr_reader = easyocr.Reader(['en'], gpu=False)
            print("✅ EasyOCR ready")
            return True
        except Exception as e:
            print(f"❌ EasyOCR failed: {e}")
            return False
    return EASYOCR_OK

def extract_text_easyocr(image_path):
    """Extract text using EasyOCR"""
    try:
        results = ocr_reader.readtext(image_path)
        texts = []
        confidences = []
        
        for (bbox, text, confidence) in results:
            if confidence > 0.3:
                texts.append(text)
                confidences.append(confidence * 100)
        
        full_text = ' '.join(texts)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0
        
        return full_text.strip(), avg_confidence
    except Exception as e:
        print(f"EasyOCR error: {e}")
        return "", 0

def extract_text_tesseract(image_path):
    """Extract text using Tesseract"""
    try:
        image = Image.open(image_path)
        text = pytesseract.image_to_string(image)
        return text.strip(), 85
    except Exception as e:
        print(f"Tesseract error: {e}")
        return "", 0

def process_image(image_path):
    """Process image and extract text"""
    global is_processing
    
    if is_processing:
        print("⚠️ Already processing, skipping...")
        return
    
    is_processing = True
    filename = os.path.basename(image_path)
    
    try:
        print(f"\n🔍 Processing: {filename}")
        
        if not os.path.exists(image_path):
            print("❌ File not found")
            return
        
        file_size = os.path.getsize(image_path)
        print(f"📏 Size: {file_size / 1024:.2f} KB")
        
        # Try OCR methods
        best_text = ""
        best_confidence = 0
        method = "none"
        
        start_time = time.time()
        
        # Try EasyOCR first
        if EASYOCR_OK and ocr_reader:
            text, confidence = extract_text_easyocr(image_path)
            if confidence > best_confidence:
                best_text, best_confidence, method = text, confidence, "EasyOCR"
        
        # Try Tesseract if needed
        if TESSERACT_OK and (not best_text or best_confidence < 70):
            text, confidence = extract_text_tesseract(image_path)
            if confidence > best_confidence:
                best_text, best_confidence, method = text, confidence, "Tesseract"
        
        processing_time = time.time() - start_time
        
        # Display results
        print("\n" + "="*60)
        print("✅ OCR COMPLETED")
        print("="*60)
        print(f"📝 Method: {method}")
        print(f"📊 Confidence: {best_confidence:.2f}%")
        print(f"⏱️ Time: {processing_time:.2f}s")
        print("📝 Text:")
        print("-"*40)
        print(best_text or "[No text detected]")
        print("-"*40)
        print(f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}")
        print("="*60)
        
        # Send to WebSocket clients
        result = {
            'type': 'ocr_result',
            'timestamp': datetime.now().isoformat(),
            'filename': filename,
            'text': best_text or "[No text detected]",
            'confidence': best_confidence,
            'method': method,
            'processingTime': processing_time
        }
        
        # Send to WebSockets (non-blocking)
        if websocket_clients:
            threading.Thread(target=send_to_websockets_sync, args=(result,)).start()
        
    except Exception as e:
        print(f"❌ Processing failed: {e}")
    finally:
        is_processing = False

async def broadcast_message(data):
    """Broadcast message to all clients"""
    if not websocket_clients:
        return
    
    message = json.dumps(data)
    clients_copy = websocket_clients.copy()
    
    for client in clients_copy:
        try:
            await client.send(message)
        except websockets.exceptions.ConnectionClosed:
            websocket_clients.discard(client)
        except Exception as e:
            print(f"Error sending to client: {e}")
            websocket_clients.discard(client)

def send_to_websockets_sync(data):
    """Send data to WebSocket clients from a thread"""
    if not websocket_clients:
        return
    
    message = json.dumps(data)
    clients_copy = websocket_clients.copy()
    
    for client in clients_copy:
        try:
            # Use asyncio.run to send the message
            asyncio.run(client.send(message))
        except Exception as e:
            print(f"Error sending to client: {e}")
            websocket_clients.discard(client)

class FileWatcher(FileSystemEventHandler):
    """Watch for new files"""
    
    def on_created(self, event):
        if not event.is_directory and self.is_image(event.src_path):
            time.sleep(1)
            threading.Thread(target=process_image, args=(event.src_path,)).start()
    
    def is_image(self, path):
        extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}
        return Path(path).suffix.lower() in extensions

def find_latest_image():
    """Find the most recent image"""
    try:
        if not os.path.exists(WATCH_DIR):
            print(f"❌ Directory not found: {WATCH_DIR}")
            return None
        
        image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}
        images = []
        
        for file in os.listdir(WATCH_DIR):
            if Path(file).suffix.lower() in image_extensions:
                full_path = os.path.join(WATCH_DIR, file)
                if os.path.isfile(full_path):
                    images.append({
                        'path': full_path,
                        'name': file,
                        'mtime': os.path.getmtime(full_path)
                    })
        
        if not images:
            print("⚠️ No images found")
            return None
        
        images.sort(key=lambda x: x['mtime'], reverse=True)
        latest = images[0]
        
        print(f"📂 Found {len(images)} images")
        print(f"🕐 Latest: {latest['name']}")
        
        return latest['path']
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

# Flask app
app = Flask(__name__)
CORS(app)

@app.route('/health')
def health():
    return jsonify({
        'status': 'running',
        'clients': len(websocket_clients),
        'engines': {
            'tesseract': TESSERACT_OK,
            'easyocr': EASYOCR_OK
        }
    })

@app.route('/latest')
def latest():
    recent = find_latest_image()
    if recent:
        return jsonify({
            'found': True,
            'path': recent,
            'name': os.path.basename(recent)
        })
    return jsonify({'found': False})

@app.route('/process', methods=['POST'])
def manual_process():
    latest_img = find_latest_image()
    if latest_img:
        threading.Thread(target=process_image, args=(latest_img,)).start()
        return jsonify({'success': True})
    return jsonify({'success': False})

async def websocket_handler(websocket):
    """Handle WebSocket connections - Updated for newer websockets library"""
    print("🔌 Client connected")
    websocket_clients.add(websocket)
    
    try:
        # Send welcome message
        await websocket.send(json.dumps({
            'type': 'connected',
            'message': 'OCR server ready'
        }))
        
        # Keep connection alive and handle incoming messages
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get('type') == 'ping':
                    await websocket.send(json.dumps({'type': 'pong'}))
            except json.JSONDecodeError:
                pass
                
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        websocket_clients.discard(websocket)
        print("🔌 Client disconnected")

def run_flask():
    """Run Flask server"""
    print(f"🌐 HTTP API: http://localhost:{HTTP_PORT}")
    app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, use_reloader=False)

async def run_websocket_server():
    """Run WebSocket server"""
    global websocket_server
    print(f"🔌 WebSocket: ws://localhost:{WS_PORT}")
    
    try:
        websocket_server = await websockets.serve(
            websocket_handler, 
            "localhost", 
            WS_PORT,
            ping_interval=20,
            ping_timeout=10
        )
        print("✅ WebSocket server started")
        
        # Keep the server running
        await websocket_server.wait_closed()
        
    except Exception as e:
        print(f"❌ WebSocket server error: {e}")

def run_websocket():
    """Run WebSocket server in event loop"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_websocket_server())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"WebSocket loop error: {e}")

def main():
    """Main function"""
    print("🎯 Starting OCR Server")
    print("="*50)
    
    # Check OCR engines
    if not (TESSERACT_OK or EASYOCR_OK):
        print("❌ No OCR engines available!")
        return
    
    # Initialize EasyOCR
    if EASYOCR_OK:
        init_easyocr()
    
    # Process latest image
    latest = find_latest_image()
    if latest:
        process_image(latest)
    
    # Start file watcher
    event_handler = FileWatcher()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=False)
    observer.start()
    print(f"👀 Watching: {WATCH_DIR}")
    
    # Start Flask in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start WebSocket in background
    websocket_thread = threading.Thread(target=run_websocket, daemon=True)
    websocket_thread.start()
    
    print("\n🚀 Server ready!")
    print("📷 Take a screenshot to test")
    print("🛑 Press Ctrl+C to stop\n")
    
    try:
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
        observer.stop()
        observer.join()
        print("✅ Stopped")

if __name__ == "__main__":
    main()