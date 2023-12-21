# Use the official Python base image for Python 3.8
FROM python:3.10

# Set the working directory within the Docker image
WORKDIR /app

# Copy the code files into the Docker image
COPY . /app

# Install dependencies
RUN pip install -r requirements.txt

# Set the entry point command to run main.py
CMD ["python", "main.py"]