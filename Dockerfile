# Microsoft's official Playwright Python image — Python 3.12 + Chromium pre-installed
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Python dependencies
# playwright is already shipped by the base image; this keeps requirements.txt in sync.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application script
COPY add_meal.py .

ENTRYPOINT ["python", "add_meal.py"]
