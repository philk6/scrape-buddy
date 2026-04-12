FROM python:3.11-slim

# Install system dependencies for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    libxshmfence1 \
    libglu1-mesa \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY . .

# Create database directory (writable at runtime)
RUN mkdir -p /app/data

# Expose port (Railway sets PORT env var)
EXPOSE ${PORT:-5000}

# Use gunicorn for production with threads for background scrape workers
CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-5000} \
    --workers 2 \
    --threads 4 \
    --timeout 300 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile -
