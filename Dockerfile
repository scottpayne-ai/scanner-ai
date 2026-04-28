FROM python:3.11-slim

# Install ffmpeg and build tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Start the server
CMD ["python", "-c", "import uvicorn, os; uvicorn.run('api_server:app', host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))"]
