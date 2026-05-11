# api/index.py
import os
import json
import base64
import hashlib
import zlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

# --- Configuration ---
# Use environment variables for sensitive information.
# These should be set in your Vercel project settings.
AUTH_KEY = os.environ.get("AUTH_KEY", "YOUR_DEFAULT_AUTH_KEY_FOR_LOCAL_DEV")
APP_ID = os.environ.get("APP_ID", "YOUR_DEFAULT_APP_ID_FOR_LOCAL_DEV")
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 600  # 10 minutes for successful fetches
NEGATIVE_CACHE_TTL_SECONDS = 60  # 1 minute for error responses

# --- In-memory Cache (Ephemeral for Serverless) ---
# IMPORTANT: This cache is lost between invocations in a serverless environment.
# For persistent caching, consider Vercel KV, Redis, or a database.
CACHE = {}
NEGATIVE_CACHE = {}

# --- Helper Functions ---

def _base64_encode(input_str):
    """Encodes a string to Base64."""
    return base64.b64encode(input_str.encode('utf-8')).decode('utf-8')

def _base64_decode(input_str):
    """Decodes a Base64 string."""
    try:
        return base64.b64decode(input_str.encode('utf-8')).decode('utf-8')
    except Exception as e:
        print(f"Base64 decode error: {e}")
        return None

def _gzip_compress(input_str):
    """Compresses a string using Gzip."""
    return zlib.compress(input_str.encode('utf-8'))

def _gzip_decompress(input_bytes):
    """Decompresses Gzip bytes."""
    try:
        return zlib.decompress(input_bytes).decode('utf-8')
    except Exception as e:
        print(f"Gzip decompress error: {e}")
        return None

def _md5_hash(input_str):
    """Computes MD5 hash of a string."""
    return hashlib.md5(input_str.encode('utf-8')).hexdigest()

def _build_opts(url, method='GET', headers=None, payload=None):
    """Builds options for fetching, mimicking UrlFetchApp options."""
    opts = {
        'url': url,
        'method': method.upper(),
        'headers': headers if headers else {},
        'payload': payload,
        # 'muteHttpExceptions': True # Mimic Apps Script behavior if needed
    }
    return opts

def _resp_headers(resp):
    """Extracts relevant headers from a response object."""
    # This is a simplified mapping. For full header replication,
    # you might need to parse `resp.headers` more meticulously.
    headers = {}
    if resp.headers:
        # Copying a few common headers. Adjust as needed.
        for key in ['Content-Type', 'Content-Length', 'Cache-Control', 'Expires', 'Vary', 'ETag', 'Last-Modified']:
            if key in resp.headers:
                headers[key] = resp.headers[key]
    return headers

def _json(data, status_code=200):
    """Helper to return JSON responses."""
    return jsonify(data), status_code

def is_valid_auth(auth_header, app_id_header):
    """Validates authentication headers."""
    if not auth_header or not app_id_header:
        return False
    return auth_header == AUTH_KEY and app_id_header == APP_ID

def get_from_cache(key):
    """Retrieves data from the in-memory cache if valid."""
    if key in CACHE:
        entry = CACHE[key]
        if datetime.now() < entry['expires_at']:
            return entry['data']
        else:
            del CACHE[key]  # Remove expired entry
    return None

def set_in_cache(key, data, ttl_seconds):
    """Sets data in the in-memory cache with an expiration time."""
    CACHE[key] = {
        'data': data,
        'expires_at': datetime.now() + timedelta(seconds=ttl_seconds)
    }

def get_from_negative_cache(key):
    """Retrieves data from the negative cache if valid."""
    if key in NEGATIVE_CACHE:
        entry = NEGATIVE_CACHE[key]
        if datetime.now() < entry['expires_at']:
            return entry['data']
        else:
            del NEGATIVE_CACHE[key] # Remove expired entry
    return None

def set_in_negative_cache(key, data, ttl_seconds):
    """Sets data in the negative cache with an expiration time."""
    NEGATIVE_CACHE[key] = {
        'data': data,
        'expires_at': datetime.now() + timedelta(seconds=ttl_seconds)
    }

# --- Core Fetching Logic ---

def _fetch_url(url, method='GET', headers=None, payload=None, retries=MAX_RETRIES):
    """Fetches a single URL using requests library, with retry logic."""
    import requests # Imported locally to keep main imports cleaner

    current_headers = headers.copy() if headers else {}
    # Default headers, mimicking UrlFetchApp
    current_headers.setdefault('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')

    # Generate a cache key for the request
    cache_key_parts = [url, method.upper()]
    if current_headers:
        cache_key_parts.append(json.dumps(dict(sorted(current_headers.items())))) # Sort headers for consistent key
    if payload:
        # Payload can be string, dict, bytes. JSONify dicts for consistency.
        if isinstance(payload, dict):
            cache_key_parts.append(json.dumps(payload, sort_keys=True))
        else:
            cache_key_parts.append(str(payload))
    cache_key = _md5_hash(":".join(cache_key_parts))

    cached_response = get_from_cache(cache_key)
    if cached_response:
        print(f"Cache hit for: {url}")
        return cached_response

    try:
        print(f"Fetching: {url} with method {method}")
        response = requests.request(
            method,
            url,
            headers=current_headers,
            data=payload, # Use 'data' for form-encoded, 'json' for JSON
            # json=payload if isinstance(payload, dict) else None, # Use if payload is always dict
            timeout=30, # Set a timeout to prevent hanging requests
            allow_redirects=True # Follow redirects by default
        )

        response_data = {
            'statusCode': response.status_code,
            'headers': dict(response.headers),
            # Attempt to decode text content, fallback to bytes for binary data
            'content': response.text if response.encoding else response.content.decode('latin-1', errors='ignore'),
            'url': response.url, # Final URL after redirects
            'finalUrl': response.url # Alias for consistency
        }

        # Cache based on status code
        if 200 <= response.status_code < 300: # Successful responses
            set_in_cache(cache_key, response_data, CACHE_TTL_SECONDS)
        elif response.status_code in [404, 403, 401]: # Cache specific known errors for a short time
            negative_cache_key = _md5_hash(f"{url}:{method}") # Simpler key for negative cache
            set_in_negative_cache(negative_cache_key, response_data, NEGATIVE_CACHE_TTL_SECONDS)
            # Decide if we cache these errors in main cache too, or just negative cache
            set_in_cache(cache_key, response_data, CACHE_TTL_SECONDS) # Cache them too with regular TTL for now.

        return response_data

    except requests.exceptions.Timeout:
        print(f"Request timed out for {url}")
        error_response = {'statusCode': 408, 'content': 'Request timed out', 'url': url, 'finalUrl': url}
        set_in_cache(cache_key, error_response, CACHE_TTL_SECONDS) # Cache timeout response
        return error_response
    except requests.exceptions.RequestException as e:
        print(f"Request failed for {url}: {e}")
        if retries > 0:
            print(f"Retrying {url} ({retries} retries left)...")
            return _fetch_url(url, method, headers, payload, retries - 1)
        else:
            print(f"Max retries reached for {url}")
            error_response = {'statusCode': 500, 'content': f"Max retries reached or fatal error: {e}", 'url': url, 'finalUrl': url}
            set_in_cache(cache_key, error_response, CACHE_TTL_SECONDS) # Cache error response
            return error_response
    except Exception as e:
        print(f"An unexpected error occurred for {url}: {e}")
        error_response = {'statusCode': 500, 'content': f"An unexpected internal error occurred: {e}", 'url': url, 'finalUrl': url}
        set_in_cache(cache_key, error_response, CACHE_TTL_SECONDS) # Cache unexpected error
        return error_response


def _do_single(request_data, headers):
    """Processes a single fetch request."""
    url = request_data.get('url')
    if not url:
        return _json({'error': 'Missing "url" in request data'}, 400)

    method = request_data.get('method', 'GET').upper()
    payload = request_data.get('payload')
    fetch_headers = request_data.get('headers', {})

    # Merge fetched headers with global headers (like User-Agent)
    merged_headers = headers.copy() # Start with global headers
    merged_headers.update(fetch_headers) # Overwrite with headers from request payload

    # Handle Content-Type for JSON payload
    if payload and isinstance(payload, dict) and 'Content-Type' not in merged_headers:
        merged_headers['Content-Type'] = 'application/json'
    # If payload is string and not specified, default to text/plain
    elif payload and isinstance(payload, str) and 'Content-Type' not in merged_headers:
        merged_headers['Content-Type'] = 'text/plain'

    # Ensure method is valid
    if method not in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
        return _json({'error': f'Unsupported HTTP method: {method}'}, 400)

    response = _fetch_url(url, method, merged_headers, payload)
    return _json(response)


def _do_batch(request_data, headers):
    """Processes multiple fetch requests in batch."""
    urls_data = request_data.get('urls')
    if not urls_data or not isinstance(urls_data, list):
        return _json({'error': 'Missing or invalid "urls" array in request data'}, 400)

    # In Google Apps Script, UrlFetchApp.fetchAll is asynchronous.
    # In Python with `requests`, we typically do synchronous calls.
    # To achieve near-parallelism without full async (which adds complexity),
    # we use `concurrent.futures.ThreadPoolExecutor`.
    import concurrent.futures

    results = []
    # Use a reasonable number of workers. Adjust based on Vercel's concurrency limits and performance needs.
    # Vercel might run functions concurrently, so limiting workers per function is good.
    max_workers = min(len(urls_data), 10) # Limit workers to number of items or 10, whichever is smaller.

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url_index = {} # Map future to index in original urls_data for ordered results

        for i, item in enumerate(urls_data):
            if not isinstance(item, dict):
                print(f"Skipping invalid item in batch at index {i}: {item}")
                results.append({'error': 'Invalid item format in batch', 'original_index': i})
                continue

            url = item.get('url')
            if not url:
                print(f"Skipping item at index {i}: Missing 'url'. Item: {item}")
                results.append({'error': 'Missing "url" in batch item', 'original_index': i})
                continue

            method = item.get('method', 'GET').upper()
            payload = item.get('payload')
            fetch_headers = item.get('headers', {})

            # Merge with global headers
            merged_headers = headers.copy()
            merged_headers.update(fetch_headers)

            # Handle Content-Type for JSON payload
            if payload and isinstance(payload, dict) and 'Content-Type' not in merged_headers:
                merged_headers['Content-Type'] = 'application/json'
            elif payload and isinstance(payload, str) and 'Content-Type' not in merged_headers:
                merged_headers['Content-Type'] = 'text/plain'

            # Ensure method is valid
            if method not in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
                print(f"Skipping item at index {i}: Unsupported method {method}")
                results.append({'error': f'Unsupported HTTP method: {method}', 'original_index': i})
                continue

            # Submit the fetch task
            future = executor.submit(
                _fetch_url,
                url,
                method,
                merged_headers,
                payload
            )
            future_to_url_index[future] = i # Store index to maintain order

        # Collect results in the original order
        ordered_results = [None] * len(urls_data)
        for future in concurrent.futures.as_completed(future_to_url_index):
            index = future_to_url_index[future]
            try:
                result = future.result()
                ordered_results[index] = result
            except Exception as exc:
                print(f'Batch item at index {index} generated an exception: {exc}')
                ordered_results[index] = {
                    'error': f'Exception during processing: {exc}',
                    'url': urls_data[index].get('url', 'N/A'),
                    'original_index': index,
                    'statusCode': 500 # Indicate internal error
                }
        results = ordered_results

    return _json(results)

# --- Flask App ---
app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def handler():
    """Main handler for Vercel deployment."""
    # Get headers, checking for required auth and app ID
    auth_header = request.headers.get('X-Auth-Key')
    app_id_header = request.headers.get('X-App-Id')

    # Security Check: Diagnostic Mode
    # Check query parameters or JSON body for diagnosticMode
    diagnostic_mode_param = request.args.get('diagnosticMode')
    if not diagnostic_mode_param and request.is_json:
        diagnostic_mode_param = request.get_json().get('diagnosticMode')

    if diagnostic_mode_param and str(diagnostic_mode_param).lower() == 'true':
        print("Diagnostic Mode enabled.")
        # Return basic info without exposing secrets
        return jsonify({
            "message": "Diagnostic mode enabled.",
            "auth_key_configured": bool(AUTH_KEY != "YOUR_DEFAULT_AUTH_KEY_FOR_LOCAL_DEV"),
            "app_id_configured": bool(APP_ID != "YOUR_DEFAULT_APP_ID_FOR_LOCAL_DEV"),
            "cache_stats": {
                "active_cache_size": len(CACHE),
                "negative_cache_size": len(NEGATIVE_CACHE)
            },
            "server_time": datetime.now().isoformat()
        })

    # Security Check: Authentication
    if not is_valid_auth(auth_header, app_id_header):
        print("Authentication failed.")
        # Return a consistent error format for failed auth
        return _json({'error': 'Authentication failed. Invalid X-Auth-Key or X-App-Id.'}, 401)

    # Process request based on method
    if request.method == 'POST':
        try:
            request_data = request.get_json()
            if not request_data:
                return _json({'error': 'Invalid JSON payload'}, 400)

            # Determine if it's a single fetch or batch fetch
            if 'url' in request_data:
                return _do_single(request_data, request.headers)
            elif 'urls' in request_data:
                return _do_batch(request_data, request.headers)
            else:
                return _json({'error': 'Request must contain "url" for single fetch or "urls" for batch fetch'}, 400)

        except Exception as e:
            print(f"Error processing POST request: {e}")
            # Catch-all for unexpected errors during POST processing
            return _json({'error': f'An internal server error occurred: {e}'}, 500)

    elif request.method == 'GET':
        # GET requests are generally not used for this proxy type.
        # Return a message indicating the correct method.
        return _json({'message': 'This is a proxy endpoint. Use POST requests with a JSON payload containing "url" or "urls".'}, 405)

    else:
        # Handle any other HTTP methods not explicitly allowed
        return _json({'error': f'Method {request.method} not allowed'}, 405)

# This block is for local development. Vercel's build process handles execution.
# if __name__ == '__main__':
#     # To run locally:
#     # 1. Set environment variables:
#     #    export AUTH_KEY='your_secret_key'
#     #    export APP_ID='your_app_id'
#     # 2. Run: python api/index.py
#     # 3. Access: http://127.0.0.1:5000/ (or the port specified by PORT env var)
#     port = int(os.environ.get('PORT', 5000))
#     app.run(debug=True, host='0.0.0.0', port=port)
