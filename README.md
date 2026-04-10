# TORCS Real-Time LLM Commentary
This project generates live spoken and textual commentary from telemetry data of the TORCS racing simulator. It uses a locally deployed large language model (Granite 3.1 MoE 1B via Ollama) and a text‑to‑speech engine (pyttsx3) to produce broadcast‑style commentary in real time.

## Features
- Extracts telemetry from TORCS (speed, track position, forward sensor distance, etc.)
- Derives higher‑level features: track situation (straight / approaching corner / in corner) and track position (left / centre / right)
- Constructs a few‑shot prompt with strict formatting rules (single sentence, ≤15 words)
- Sends prompt to local LLM (Granite 3.1 MoE 1B) served by Ollama
- Displays commentary as text and plays it as speech
- Asynchronous threads ensure the simulation loop never blocks
- Logs commentary with timestamps, telemetry, and response times for evaluation

## Requirements
- Python 3.8+
- Windows OS
- Ollama
