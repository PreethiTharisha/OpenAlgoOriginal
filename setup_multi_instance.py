#!/usr/bin/env python3
"""
OpenAlgo Multi-Instance Setup Helper

This script helps you set up environment files for running multiple OpenAlgo instances.
It generates unique security keys and creates properly formatted .env files.

Usage:
    python setup_multi_instance.py
"""

import os
import sys
import secrets
from pathlib import Path


def generate_secure_key(length=32):
    """Generate a cryptographically secure random key"""
    return secrets.token_hex(length // 2)


def read_current_env():
    """Read the current .env file to copy common variables"""
    env_vars = {}
    env_file = Path(".env")
    
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    
    return env_vars


def create_instance_env(instance_num, base_port, base_ws_port, base_zmq_port, common_vars):
    """Create environment variables for a specific instance"""
    
    flask_port = base_port + (instance_num - 1)
    ws_port = base_ws_port + (instance_num - 1)
    zmq_port = base_zmq_port + (instance_num - 1)
    db_suffix = "" if instance_num == 1 else f"_{instance_num}"
    
    # Generate unique keys for this instance
    app_key = generate_secure_key(32)
    api_pepper = generate_secure_key(32)
    
    instance_vars = {
        "# ============================================================================": None,
        f"# OpenAlgo Instance {instance_num} Configuration": None,
        "# ============================================================================": None,
        "": None,
        "# Instance-specific configuration": None,
        "FLASK_PORT": str(flask_port),
        "FLASK_HOST_IP": "127.0.0.1",
        "WEBSOCKET_PORT": str(ws_port),
        "ZMQ_PORT": str(zmq_port),
        f"DATABASE_URL": f"sqlite:///db/openalgo{db_suffix}.db",
        "HOST_SERVER": f"http://127.0.0.1:{flask_port}",
        "APP_KEY": app_key,
        "API_KEY_PEPPER": api_pepper,
        "": None,
        "# Common configuration (copy from your current .env)": None,
    }
    
    # Add common variables
    for key, value in common_vars.items():
        if key not in instance_vars and key not in [
            "FLASK_PORT", "FLASK_HOST_IP", "WEBSOCKET_PORT", "ZMQ_PORT",
            "DATABASE_URL", "HOST_SERVER", "APP_KEY", "API_KEY_PEPPER"
        ]:
            instance_vars[key] = value
    
    return instance_vars, {
        "port": flask_port,
        "ws_port": ws_port,
        "zmq_port": zmq_port,
        "db": f"db/openalgo{db_suffix}.db",
        "app_key": app_key,
        "api_pepper": api_pepper,
    }


def write_env_file(filename, env_vars):
    """Write environment variables to a file"""
    with open(filename, "w") as f:
        for key, value in env_vars.items():
            if value is None:
                f.write(f"{key}\n")
            else:
                f.write(f"{key}={value}\n")


def print_summary(instances_info):
    """Print a summary of created instances"""
    print("\n" + "=" * 70)
    print("OpenAlgo Multi-Instance Setup Complete!")
    print("=" * 70)
    
    for i, info in enumerate(instances_info, 1):
        print(f"\nInstance {i}:")
        print(f"  HTTP Port:      {info['port']}")
        print(f"  WebSocket Port: {info['ws_port']}")
        print(f"  ZMQ Port:       {info['zmq_port']}")
        print(f"  Database:       {info['db']}")
        print(f"  Access URL:     http://127.0.0.1:{info['port']}")
        print(f"  APP_KEY:        {info['app_key'][:16]}...")
        print(f"  API_KEY_PEPPER: {info['api_pepper'][:16]}...")


def print_next_steps(num_instances):
    """Print next steps for the user"""
    print("\n" + "=" * 70)
    print("Next Steps:")
    print("=" * 70)
    
    print("\n1. Verify the created .env files:")
    for i in range(1, num_instances + 1):
        print(f"   - .env.instance{i}")
    
    print("\n2. Run each instance in a separate terminal:")
    print("\n   Terminal 1 (Linux/macOS):")
    print("   export $(cat .env.instance1 | xargs) && python app.py")
    print("\n   Terminal 1 (Windows PowerShell):")
    print('   Get-Content .env.instance1 | ForEach-Object { if ($_ -match')
    print('   \'^\s*([^=]+)=(.*)$\') { [Environment]::SetEnvironmentVariable')
    print('   ($matches[1], $matches[2]) } }; python app.py')
    
    if num_instances >= 2:
        print("\n   Terminal 2 (Linux/macOS):")
        print("   export $(cat .env.instance2 | xargs) && python app.py")
        print("\n   Terminal 2 (Windows PowerShell):")
        print('   Get-Content .env.instance2 | ForEach-Object { if ($_ -match')
        print('   \'^\s*([^=]+)=(.*)$\') { [Environment]::SetEnvironmentVariable')
        print('   ($matches[1], $matches[2]) } }; python app.py')
    
    print("\n3. Access your instances:")
    for i in range(1, num_instances + 1):
        port = 5000 + (i - 1)
        print(f"   Instance {i}: http://127.0.0.1:{port}")
    
    print("\n4. Check port usage (Linux/macOS):")
    print("   lsof -i :5000")
    print("   lsof -i :5001")
    
    print("\n5. Check port usage (Windows):")
    print("   netstat -ano | findstr :5000")
    print("   netstat -ano | findstr :5001")
    
    print("\n" + "=" * 70)


def main():
    """Main entry point"""
    print("=" * 70)
    print("OpenAlgo Multi-Instance Setup Helper")
    print("=" * 70)
    
    # Ask user how many instances they want
    while True:
        try:
            num_instances = int(input("\nHow many instances do you want to run? (2-10): "))
            if 2 <= num_instances <= 10:
                break
            print("Please enter a number between 2 and 10")
        except ValueError:
            print("Please enter a valid number")
    
    # Ask for base ports
    print("\nEnter base port numbers (each instance will use base + instance_number - 1):")
    
    while True:
        try:
            base_port = int(input("Base HTTP port (default 5000): ") or "5000")
            if 1024 <= base_port <= 65535 - num_instances:
                break
            print(f"Please enter a port between 1024 and {65535 - num_instances}")
        except ValueError:
            print("Please enter a valid number")
    
    while True:
        try:
            base_ws_port = int(input("Base WebSocket port (default 8765): ") or "8765")
            if 1024 <= base_ws_port <= 65535 - num_instances:
                break
            print(f"Please enter a port between 1024 and {65535 - num_instances}")
        except ValueError:
            print("Please enter a valid number")
    
    while True:
        try:
            base_zmq_port = int(input("Base ZMQ port (default 5555): ") or "5555")
            if 1024 <= base_zmq_port <= 65535 - num_instances:
                break
            print(f"Please enter a port between 1024 and {65535 - num_instances}")
        except ValueError:
            print("Please enter a valid number")
    
    # Read current .env file
    print("\nReading current .env file...")
    common_vars = read_current_env()
    
    if not common_vars:
        print("Warning: No .env file found. You'll need to add common variables manually.")
    else:
        print(f"Found {len(common_vars)} variables in .env file")
    
    # Create environment files
    print(f"\nCreating {num_instances} environment files...")
    instances_info = []
    
    for i in range(1, num_instances + 1):
        env_vars, info = create_instance_env(i, base_port, base_ws_port, base_zmq_port, common_vars)
        filename = f".env.instance{i}"
        write_env_file(filename, env_vars)
        instances_info.append(info)
        print(f"✓ Created {filename}")
    
    # Print summary
    print_summary(instances_info)
    
    # Print next steps
    print_next_steps(num_instances)
    
    print("\nFor more information, see:")
    print("  - QUICK_START_MULTI_INSTANCE.md")
    print("  - MULTI_INSTANCE_SETUP.md")
    print("  - MULTI_INSTANCE_WINDOWS.md")
    print("  - MULTI_INSTANCE_CHEATSHEET.md")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
