"""
Setup script for Frappe Microservice Framework
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="frappe-microservice",
    version="1.0.0",
    author="Vyogo Technologies",
    author_email="dev@frappe.io",
    description="A Python framework for building secure, isolated Frappe microservices",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your-org/frappe-microservice",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=[
        "flask>=2.0.0",
        "requests>=2.25.0",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-instrumentation-flask",
        "opentelemetry-exporter-otlp",
    ],
    extras_require={
        "dev": [
            "pytest>=6.0",
            "pytest-cov>=2.0",
            "black>=21.0",
            "flake8>=3.9",
            "behave",
            "allure-pytest",
            "allure-behave",
            "responses",
        ],
    },
    entry_points={
        "console_scripts": [
            "frappe-ms=frappe_microservice.cli:main",
        ],
    },
)
