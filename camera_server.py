"""
Run this locally (your PC/Raspberry Pi) — NOT on Vercel.
It handles webcam, MediaPipe, and speech, then POSTs state to Vercel.

Install locally:
  pip install opencv-python mediapipe SpeechRecognition numpy requests
"""

import cv2
import mediapipe as mp
import numpy as np
import math
import time
import threading
import requests
import speech_recognition as sr

VERCEL_URL = "https://your-app.vercel.app"  # ← change this

# ── Camera & ML setup ────────────────────────────────────────────────────────

camera = cv2.VideoCapture(0)
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
smile_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_smile.xml')
eye_cascade   = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

mp_hands = mp.solutions.hands
hands    = mp_hands.Hands(min_detection_confidence=0.7)

recognizer = sr.Recognizer()
is_listening = False
current_speech = ""

state = {"face": False, "expression": "neutral", "gesture": "none",
         "attendance": "Absent", "speech": "", "listening": False}

# ── Helpers ──────────────────────────────────────────────────────────────────

def fingers_up(hand, label):
    tips = [4, 8, 12, 16, 20]
    f = []
    f.append(hand.landmark[4].x < hand.landmark[3].x if label == "Right"
             else hand.landmark[4].x > hand.landmark[3].x)
    for i in range(1, 5):
        f.append(hand.landmark[tips[i]].y < hand.landmark[tips[i]-2].y)
    return f

def detect_gesture(hand, label):
    f = fingers_up(hand, label)
    mapping = {
        (0,0,0,0,0): "✊", (1,1,1,1,1): "🤚", (1,0,0,0,0): "👍",
        (0,1,1,0,0): "✌️", (0,1,0,0,0): "☝️", (0,1,1,1,0): "🤟"
    }
    g = mapping.get(tuple(f))
    if g: return g
    t, i = hand.landmark[4], hand.landmark[8]
    if math.hypot(t.x-i.x, t.y-i.y) < 0.04: return "👌"
    return "none"

# ── Push state to Vercel every second ────────────────────────────────────────

def push_state():
    while True:
        try:
            requests.post(f"{VERCEL_URL}/update-status", json=state, timeout=2)
        except Exception as e:
            print(f"Push error: {e}")
        time.sleep(1)

# ── Speech listener ──────────────────────────────────────────────────────────

def speech_listener():
    global current_speech
    mic = sr.Microphone()
    recognizer.energy_threshold = 300
    with mic as src:
        recognizer.adjust_for_ambient_noise(src, duration=1)
    while True:
        if is_listening:
            try:
                with mic as src:
                    audio = recognizer.listen(src, timeout=5, phrase_time_limit=5)
                current_speech = recognizer.recognize_google(audio)
                state["speech"] = current_speech
                print("Heard:", current_speech)
            except sr.UnknownValueError:
                pass
            except Exception as e:
                print("Speech error:", e)
        time.sleep(0.2)

# ── Main loop ────────────────────────────────────────────────────────────────

threading.Thread(target=push_state, daemon=True).start()
threading.Thread(target=speech_listener, daemon=True).start()

absent_count = 0

while True:
    ok, frame = camera.read()
    if not ok: break
    frame = cv2.flip(frame, 1)
    small = cv2.resize(frame, (320, 240))
    gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    rgb   = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

    faces = face_cascade.detectMultiScale(gray, 1.3, 5)
    face_detected = len(faces) > 0
    expression = "neutral"

    for (x, y, w, h) in faces:
        roi = cv2.cvtColor(gray[y:y+h, x:x+w], cv2.COLOR_GRAY2BGR)
        roi_gray = gray[y:y+h, x:x+w]
        smiles = smile_cascade.detectMultiScale(roi_gray, 1.8, 20)
        eyes   = eye_cascade.detectMultiScale(roi_gray, 1.3, 10)
        if len(smiles) > 0:       expression = "Smile 😊"
        elif len(eyes) == 0:      expression = "Angry 😠"
        elif len(eyes) == 1:      expression = "Sad 😞"
        elif len(eyes) >= 2:      expression = "Stunned 😲"

    gesture = "none"
    result = hands.process(rgb)
    if result.multi_hand_landmarks and result.multi_handedness:
        for lm, hd in zip(result.multi_hand_landmarks, result.multi_handedness):
            label = hd.classification[0].label
            gesture = f"{label}:{detect_gesture(lm, label)}"

    absent_count = 0 if face_detected else absent_count + 1
    attendance = "Absent" if absent_count >= 5000 else ("Present" if face_detected else "Absent")

    state.update({"face": face_detected, "expression": expression,
                  "gesture": gesture, "attendance": attendance,
                  "listening": is_listening})

    cv2.imshow("Camera (local)", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

camera.release()
cv2.destroyAllWindows()
