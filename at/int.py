import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
import os
import pickle
import pandas as pd
from datetime import datetime
from win32com.client import Dispatch
import cv2
import numpy as np
from keras_facenet import FaceNet
from scipy.spatial.distance import cosine
import subprocess
from collections import Counter
import shutil
import logging
import tensorflow as tf

# Suppress TensorFlow warnings
tf.get_logger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

ADMIN_PASSWORD = "1234"

# Text-to-Speech helper
def speak(text):
    try:
        speaker = Dispatch("SAPI.SpVoice")
        speaker.Speak(text)
    except:
        pass  # Silently ignore if text-to-speech fails

class AttendanceApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Face Attendance System")
        self.root.geometry("600x550")

        self.current_user = None
        self.facedetect = cv2.CascadeClassifier('Data/haarcascade_frontalface_default.xml')
        if self.facedetect.empty():
            messagebox.showerror("Error", "Failed to load haarcascade_frontalface_default.xml. Ensure the file exists in the Data directory.")
            self.root.destroy()
            return

        try:
            self.facenet = FaceNet()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to initialize FaceNet: {str(e)}")
            self.root.destroy()
            return

        self.labels = []
        self.embeddings = []
        self.load_face_data()

        self.build_form()

    def load_face_data(self):
        try:
            if os.path.exists('Data/names.pkl') and os.path.exists('Data/faces_data.pkl'):
                with open('Data/names.pkl', 'rb') as w:
                    self.labels = pickle.load(w)
                with open('Data/faces_data.pkl', 'rb') as f:
                    self.embeddings = pickle.load(f)

                # Validate data consistency
                if len(self.labels) != self.embeddings.shape[0]:
                    response = messagebox.askyesno(
                        "Data Mismatch",
                        f"Mismatch between names ({len(self.labels)}) and face samples ({self.embeddings.shape[0]}). "
                        "Would you like to reset the data files?"
                    )
                    if response:
                        os.remove('Data/names.pkl')
                        os.remove('Data/faces_data.pkl')
                        messagebox.showinfo("Reset", "Data files have been reset. Please enroll users again.")
                        self.labels = []
                        self.embeddings = []
                    else:
                        messagebox.showerror("Error", "Cannot proceed due to data mismatch.")
                    return

                if len(self.labels) < 10:
                    messagebox.showwarning("Warning", "Insufficient data for recognition. Enroll more users.")
                    return

                print(f"Loaded {len(self.labels)} face samples: {self.labels}")
            else:
                messagebox.showwarning("Warning", "No face data found. Please enroll users first.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load face data: {str(e)}")

    def build_form(self):
        title = tk.Label(self.root, text="🎯 Face Attendance System", font=("Arial", 18, "bold"))
        title.pack(pady=20)

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="🔐 Authenticate & Enroll", command=self.authenticate_user, bg="#4CAF50", fg="white", font=("Arial", 12)).grid(row=0, column=0, padx=10)
        tk.Button(btn_frame, text="📝 Mark Attendance", command=self.add_today_attendance, bg="#FF5722", fg="white", font=("Arial", 12)).grid(row=0, column=1, padx=10)
        tk.Button(btn_frame, text="📅 View Attendance", command=self.view_attendance, bg="#2196F3", fg="white", font=("Arial", 12)).grid(row=0, column=2, padx=10)
        tk.Button(btn_frame, text="🗑️ Clear Data", command=self.clear_data, bg="#F44336", fg="white", font=("Arial", 12)).grid(row=1, column=1, padx=10, pady=10)

        self.tree = ttk.Treeview(self.root, columns=("Name", "Time", "Date"), show="headings", height=15)
        for col in ("Name", "Time", "Date"):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=180)
        self.tree.pack(pady=10, fill="both", expand=True)

    def authenticate_user(self):
        pwd = simpledialog.askstring("Admin Authentication", "Enter admin password:", show="*")
        if pwd != ADMIN_PASSWORD:
            messagebox.showerror("Authentication Failed", "Incorrect admin password.")
            return
        self.open_enroll_window()

    def clear_data(self):
        pwd = simpledialog.askstring("Admin Authentication", "Enter admin password to clear face and attendance data:", show="*")
        if pwd != ADMIN_PASSWORD:
            messagebox.showerror("Authentication Failed", "Incorrect admin password.")
            return
        try:
            if os.path.exists('Data/names.pkl'):
                os.remove('Data/names.pkl')
                print("Deleted names.pkl")
            if os.path.exists('Data/faces_data.pkl'):
                os.remove('Data/faces_data.pkl')
                print("Deleted faces_data.pkl")
            attendance_dir = 'Attendance'
            if os.path.exists(attendance_dir):
                shutil.rmtree(attendance_dir)
                print(f"Deleted attendance directory: {attendance_dir}")

            messagebox.showinfo("Success", "All face and attendance data has been cleared. Please enroll users again.")
            self.load_face_data()
            self.view_attendance()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to clear data: {str(e)}")

    def open_enroll_window(self):
        win = tk.Toplevel(self.root)
        win.title("Enroll New User")
        win.geometry("400x200")

        tk.Label(win, text="Enter Name:", font=("Arial", 12)).pack(pady=5)
        name_var = tk.StringVar()
        tk.Entry(win, textvariable=name_var, width=30).pack(pady=5)

        def enroll():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Input Error", "Please enter a name.")
                return
            names = []
            if os.path.exists('Data/names.pkl'):
                with open('Data/names.pkl', 'rb') as f:
                    try:
                        names = pickle.load(f)
                    except:
                        names = []
            if name in names:
                messagebox.showerror("Input Error", "User name already enrolled.")
                win.destroy()
                return

            self.current_user = name
            win.destroy()
            try:
                result = subprocess.run(
                    ["python", "Add_faces.py", name],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if result.returncode != 0:
                    error_msg = result.stderr
                    if "This face is already registered" in error_msg:
                        messagebox.showerror("Error", f"Face data capture failed: This face is already registered as another user.")
                    else:
                        messagebox.showerror("Error", f"Face data capture failed: {error_msg}")
                    return
                messagebox.showinfo("Enrolled", f"{name} enrolled successfully.")
                self.load_face_data()
            except subprocess.TimeoutExpired:
                messagebox.showerror("Error", "Face capture timed out.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to run Add_faces.py: {str(e)}")

        tk.Button(win, text="Add Face Data", command=enroll, bg="#4CAF50", fg="white", font=("Arial", 12)).pack(pady=15)

    def recognize_face(self):
        if len(self.embeddings) == 0:
            print("No face data available for recognition.")
            messagebox.showerror("Error", "No enrolled users. Please enroll users first.")
            return None

        print("Attempting to initialize camera...")
        video = cv2.VideoCapture(0)
        if not video.isOpened():
            print("Error: Could not open video capture.")
            messagebox.showerror("Error", "Could not open video capture. Ensure the camera is available.")
            return None

        print("Camera initialized successfully.")
        start_time = datetime.now()
        timeout = 15
        predictions = []
        confidences = []

        try:
            while (datetime.now() - start_time).seconds < timeout:
                ret, frame = video.read()
                if not ret:
                    print("Warning: Failed to capture frame.")
                    continue

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = self.facedetect.detectMultiScale(gray, 1.3, 5)

                if len(faces) == 0:
                    print("No faces detected in frame.")

                for (x, y, w, h) in faces:
                    face = frame[y:y+h, x:x+w]
                    face = cv2.resize(face, (160, 160))
                    embedding = self.facenet.embeddings([face])[0]

                    min_distance = float('inf')
                    recognized_name = None
                    for i, stored_embedding in enumerate(self.embeddings):
                        distance = cosine(embedding, stored_embedding)
                        if distance < min_distance:
                            min_distance = distance
                            recognized_name = self.labels[i]

                    if min_distance < 0.5:  # Threshold for recognition
                        confidence = 1 - min_distance  # Convert distance to confidence
                        predictions.append(recognized_name)
                        confidences.append(confidence)
                        print(f"Frame prediction: {recognized_name} (confidence: {confidence:.2f})")
                        cv2.putText(frame, f"{recognized_name} ({confidence:.2f})", (x, y-15), cv2.FONT_HERSHEY_COMPLEX, 1, (255, 255, 255), 1)
                        cv2.rectangle(frame, (x, y), (x+w, y+h), (50, 50, 255), 2)

                        if len(predictions) >= 10:
                            most_common = Counter(predictions).most_common(1)
                            if most_common[0][1] >= 6:  # At least 6/10 frames agree
                                avg_confidence = np.mean([conf for pred, conf in zip(predictions, confidences) if pred == most_common[0][0]])
                                if avg_confidence >= 0.75:
                                    print(f"Recognition successful: {most_common[0][0]} with {most_common[0][1]} votes, avg confidence {avg_confidence:.2f}")
                                    video.release()
                                    cv2.destroyAllWindows()
                                    return most_common[0][0]

                cv2.imshow("Frame", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
        except Exception as e:
            print(f"Recognition error: {str(e)}")
            messagebox.showerror("Error", f"Face recognition failed: {str(e)}")

        video.release()
        cv2.destroyAllWindows()
        print("Recognition timed out or Insufficient predictions.")

        if predictions:
            most_common = Counter(predictions).most_common(1)
            if most_common[0][1] >= 4:  # Fallback: at least 4/10 frames agree
                avg_confidence = np.mean([conf for pred, conf in zip(predictions, confidences) if pred == most_common[0][0]])
                if avg_confidence >= 0.75:
                    print(f"Fallback recognition: {most_common[0][0]} with {most_common[0][1]} votes, avg confidence {avg_confidence:.2f}")
                    return most_common[0][0]

        print("No reliable recognition achieved.")
        return None

    def add_today_attendance(self):
        self.current_user = None
        print("Starting attendance marking process...")
        recognized_user = self.recognize_face()
        if not recognized_user:
            print("Attendance marking failed: No user recognized.")
            messagebox.showerror("Recognition Failed", "User not recognized. Please ensure your face is visible and try again.")
            return

        # Prompt for confirmation for all users
        confirm = messagebox.askyesno(
            "Confirm User",
            f"Recognized as {recognized_user}. Is this correct?"
        )
        if not confirm:
            print("User rejected recognition.")
            messagebox.showinfo("Retry", "Please try again with a clearer face position.")
            return

        self.current_user = recognized_user
        print(f"User признан: {self.current_user}")

        time_now = datetime.now().strftime("%H:%M:%S")
        date_now = datetime.now().strftime("%d-%m-%Y")
        filename = f"Attendance/Attendance_{date_now}.csv"
        os.makedirs("Attendance", exist_ok=True)
        exist = os.path.exists(filename)

        if exist:
            try:
                df = pd.read_csv(filename)
                recent_attendance = df[df['Name'] == self.current_user]
                if not recent_attendance.empty:
                    last_time = pd.to_datetime(recent_attendance['Time'].iloc[-1], format="%H:%M:%S")
                    if (datetime.now() - last_time).total_seconds() < 3600:
                        print(f"Duplicate attendance detected for {self.current_user}.")
                        messagebox.showinfo("Duplicate", f"Attendance already marked for {self.current_user} within the last hour.")
                        self.current_user = None
                        return
            except Exception as e:
                print(f"Error: Failed to read attendance file - {str(e)}")
                messagebox.showerror("Error", f"Failed to read attendance file: {str(e)}")
                return

        try:
            with open(filename, "a") as f:
                if not exist:
                    f.write("Name,Time,Date\n")
                f.write(f"{self.current_user},{time_now},{date_now}\n")
        except Exception as e:
            print(f"Error: Failed to write to attendance file - {str(e)}")
            messagebox.showerror("Error", f"Failed to write to attendance file: {str(e)}")
            return

        print(f"Attendance marked for {self.current_user} at {time_now}.")
        speak(f"{self.current_user}, attendance is marked")
        messagebox.showinfo("Success", f"Attendance marked for {self.current_user} at {time_now}.")
        self.view_attendance()
        self.current_user = None

    def view_attendance(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        attendance_dir = 'Attendance'
        if not os.path.exists(attendance_dir):
            messagebox.showwarning("Not Found", "No attendance files found.")
            return
        files = sorted(os.listdir(attendance_dir), reverse=True)
        try:
            for file in files:
                df = pd.read_csv(os.path.join(attendance_dir, file))
                for index, row in df.iterrows():
                    self.tree.insert("", "end", values=(row['Name'], row['Time'], row['Date']))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load attendance: {str(e)}")

if __name__ == "__main__":
    root = tk.Tk()
    app = AttendanceApp(root)
    root.mainloop()