import smtplib
from email.mime.text import MIMEText

SENDER_EMAIL = "printesh3d@gmail.com"
SENDER_PASSWORD = "uvhw qeli nhzq vkyg"
RECIPIENT = "arunaway@berkeley.edu"

msg = MIMEText("hello world")
msg["Subject"] = "hello world"
msg["From"] = SENDER_EMAIL
msg["To"] = RECIPIENT

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(SENDER_EMAIL, SENDER_PASSWORD)
    server.sendmail(SENDER_EMAIL, RECIPIENT, msg.as_string())

print("Email sent successfully.")
