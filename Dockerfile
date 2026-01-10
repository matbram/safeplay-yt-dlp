FROM python:3.11-slim

# No ffmpeg needed - ElevenLabs accepts video files directly!

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create temp directory
RUN mkdir -p /tmp/safeplay-downloads

# Expose port
EXPOSE 3002

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3002"]
