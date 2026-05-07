from setuptools import find_packages, setup


setup(
    name="signal-room",
    version="0.1.0",
    description="Curious Endeavor Signal Room CLI",
    packages=find_packages(include=["signal_room", "signal_room.*"]),
    install_requires=[
        "fastapi>=0.111,<1",
        "jinja2>=3.1,<4",
        "psycopg[binary]>=3.2,<4",
        "python-multipart>=0.0.9,<1",
        "requests>=2.32,<3",
        "uvicorn[standard]>=0.30,<1",
    ],
    entry_points={
        "console_scripts": [
            "signal-room=signal_room.cli:main",
        ]
    },
)
