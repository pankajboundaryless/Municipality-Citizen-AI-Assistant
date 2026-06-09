#!/bin/bash
gunicorn -w 1 -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 600 \
  --keep-alive 75 \
  main:app
