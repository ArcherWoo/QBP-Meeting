from .app import create_app
from .models import Meeting, User, db


def init_database():
    app = create_app("development")
    with app.app_context():
        app.ensure_database()
        print("QBP Meeting MGMT database initialized.")
        print("Login: admin / admin123")


if __name__ == "__main__":
    init_database()
