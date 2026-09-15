import pandas as pd
from sklearn.linear_model import LogisticRegression
import pickle

# Dummy training data
data = {
    "attendance": [80, 60, 90, 40, 70, 85, 50],
    "marks": [75, 50, 88, 35, 60, 92, 45],
    "label": [1, 0, 1, 0, 1, 1, 0]  # 1 = pass, 0 = fail
}
df = pd.DataFrame(data)

X = df[["attendance", "marks"]]
y = df["label"]

# Train model
model = LogisticRegression()
model.fit(X, y)

# Save model
with open("student_model.pkl", "wb") as f:
    pickle.dump(model, f)

print("✅ Model trained and saved as student_model.pkl")
