import cv2
import pickle
import numpy as np
import os
import sys
import time
from keras_facenet import FaceNet
from scipy.spatial.distance import cosine
from collections import Counter
import logging
import tensorflow as tf

# Suppress TensorFlow warnings
tf.get_logger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

# Initialize video capture and face detector
video = cv2.VideoCapture(0)
if not video.isOpened():
    print("Error: Could not open video capture.")
    exit()

facedetect = cv2.CascadeClassifier('Data/haarcascade_frontalface_default.xml')
if facedetect.empty():
    print("Error: Could not load haarcascade_frontalface_default.xml.")
    video.release()
    exit()

try:
    facenet = FaceNet()
except Exception as e:
    print(f"Error: Failed to initialize FaceNet: {str(e)}")
    video.release()
    exit()

faces_data = []
i = 0

# Get name from command line argument or prompt
if len(sys.argv) > 1:
    name = sys.argv[1].strip()
else:
    name = input("Enter your name: ").strip()

if not name:
    print("Error: Name cannot be empty.")
    video.release()
    cv2.destroyAllWindows()
    exit()

# Check if name already exists
os.makedirs('Data', exist_ok=True)
if os.path.exists('Data/names.pkl'):
    with open('Data/names.pkl', 'rb') as f:
        try:
            names = pickle.load(f)
        except:
            names = []
    if name in names:
        print(f"User {name} already enrolled. Face data not updated.")
        video.release()
        cv2.destroyAllWindows()
        exit()

# Capture face data with diversity
print("Please adjust your face for each capture (e.g., tilt head left/right, look up/down, vary lighting).")
while len(faces_data) < 10:
    ret, frame = video.read()
    if not ret:
        print("Error: Failed to capture frame.")
        break

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = facedetect.detectMultiScale(gray, 1.3, 5)

    for (x, y, w, h) in faces:
        crop_img = frame[y:y+h, x:x+w]
        resized_img = cv2.resize(crop_img, (160, 160))
        embedding = facenet.embeddings([resized_img])[0]

        if i % 15 == 0:
            faces_data.append(embedding)
            print(f"Captured face sample {len(faces_data)}/10 for {name}")
            time.sleep(2)
            cv2.putText(frame, f"Sample {len(faces_data)}/10: Adjust face", (50, 100), cv2.FONT_HERSHEY_COMPLEX, 0.8, (0, 255, 0), 1)

        i += 1
        cv2.putText(frame, f"Samples: {len(faces_data)}/10", (50, 50), cv2.FONT_HERSHEY_COMPLEX, 1, (50, 50, 255), 1)
        cv2.rectangle(frame, (x, y), (x+w, y+h), (50, 50, 255), 1)

    cv2.imshow("Frame", frame)

    if cv2.waitKey(1) == ord('q'):
        break

video.release()
cv2.destroyAllWindows()

# Validate collected data
if len(faces_data) != 10:
    print(f"Error: Collected {len(faces_data)} face samples, expected 10. No data saved.")
    exit()

# Convert faces_data to numpy array
faces_data = np.array(faces_data)

# Check if face already exists
if os.path.exists('Data/names.pkl') and os.path.exists('Data/faces_data.pkl'):
    try:
        with open('Data/names.pkl', 'rb') as f:
            existing_names = pickle.load(f)
        with open('Data/faces_data.pkl', 'rb') as f:
            existing_faces = pickle.load(f)

        if len(existing_names) >= 10:
            predictions = []
            confidences = []
            for face in faces_data:
                min_distance = float('inf')
                pred = None
                for i, stored_face in enumerate(existing_faces):
                    distance = cosine(face, stored_face)
                    if distance < min_distance:
                        min_distance = distance
                        pred = existing_names[i]
                if min_distance < 0.5:
                    confidence = 1 - min_distance
                    predictions.append(pred)
                    confidences.append(confidence)

            most_common = Counter(predictions).most_common(1)
            if most_common[0][1] >= 6:  # At least 6/10 samples match
                avg_confidence = np.mean([conf for pred, conf in zip(predictions, confidences) if pred == most_common[0][0]])
                if avg_confidence >= 0.75:
                    print(f"Error: This face is already registered as {most_common[0][0]} with {most_common[0][1]} matches, average confidence {avg_confidence:.2f}.")
                    exit()
    except Exception as e:
        print(f"Warning: Could not check for existing face: {str(e)}. Proceeding with enrollment.")

# Save names and face data atomically
temp_names = [name] * 10
temp_faces = faces_data

if os.path.exists('Data/names.pkl'):
    with open('Data/names.pkl', 'rb') as f:
        try:
            existing_names = pickle.load(f)
        except:
            existing_names = []
    temp_names = existing_names + temp_names

if os.path.exists('Data/faces_data.pkl'):
    with open('Data/faces_data.pkl', 'rb') as f:
        try:
            existing_faces = pickle.load(f)
        except:
            existing_faces = np.empty((0, 512), dtype=np.float32)
    temp_faces = np.append(existing_faces, faces_data, axis=0)

# Validate before saving
if len(temp_names) != temp_faces.shape[0]:
    print(f"Error: Mismatch between names ({len(temp_names)}) and face samples ({temp_faces.shape[0]}). No data saved.")
    exit()

# Save to pickle files
with open('Data/names.pkl', 'wb') as f:
    pickle.dump(temp_names, f)
with open('Data/faces_data.pkl', 'wb') as f:
    pickle.dump(temp_faces, f)

print(f"Successfully enrolled {name} with 10 face samples.")
print(f"Current names in names.pkl: {temp_names}")