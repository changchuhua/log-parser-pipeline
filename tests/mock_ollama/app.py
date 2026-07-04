import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class MockOllamaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        if self.path == '/v1/chat/completions' or self.path == '/api/chat':
            response_data = {
                "choices": [{"message": {"content": "User <*> logged in"}}],
                "model": "llama3",
                "message": {"role": "assistant", "content": "User <*> logged in"}
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode('utf-8'))
            
        elif self.path == '/v1/embeddings' or self.path == '/api/embeddings':
            response_data = {
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
                "embedding": [0.1, 0.2, 0.3]
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode('utf-8'))
            
        else:
            self.send_response(404)
            self.end_headers()

def run(port=11434):
    server_address = ('', port)
    httpd = HTTPServer(server_address, MockOllamaHandler)
    print(f"[*] Starting Mock Ollama Service on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
