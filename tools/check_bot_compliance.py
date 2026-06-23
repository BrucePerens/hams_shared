#!/usr/bin/env python3
import socket
import urllib.request
import json
import sys

def get_public_ip():
    try:
        req = urllib.request.Request('https://api.ipify.org?format=json', headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req, timeout=5).read()
        return json.loads(response)['ip']
    except Exception as e:
        print(f"Error fetching public IP: {e}")
        return None

def check_fcrdns(ip):
    try:
        # 1. Reverse lookup
        host, aliases, ips = socket.gethostbyaddr(ip)
        print(f"Reverse DNS (PTR) check passed for IP {ip}.")
        print(f"Resolved hostname: {host}")

        # 2. Forward lookup
        try:
            forward_ip = socket.gethostbyname(host)
            if forward_ip == ip:
                print(f"Forward DNS (A) check passed. Hostname {host} resolves back to {ip}.")
                return True
            else:
                print(f"Forward DNS (A) check failed. Hostname {host} resolves to {forward_ip}, not {ip}.")
                return False
        except socket.gaierror as e:
            print(f"Forward DNS (A) check failed for hostname {host}: {e}")
            return False

    except socket.herror as e:
        print(f"Reverse DNS (PTR) check failed for IP {ip}: {e}")
        return False
    except Exception as e:
        print(f"Error checking Reverse DNS for IP {ip}: {e}")
        return False

def main():
    print("Checking host bot compliance...")

    # 1. Forward-Confirmed Reverse DNS Check
    ip = get_public_ip()
    if not ip:
        print("Cannot verify reverse DNS without public IP.")
        sys.exit(1)

    print(f"Public IP detected: {ip}")
    has_fcrdns = check_fcrdns(ip)

    if not has_fcrdns:
        print("\nWARNING: This host lacks a valid Forward-Confirmed Reverse DNS (FCrDNS) setup.")
        print("Without FCrDNS, Cloudflare, Akamai, and other CDNs may block automated requests.")
        print("Please configure a PTR record with your hosting provider and ensure it points back to this IP.")
        sys.exit(1)

    print("\nSUCCESS: Basic host compliance checks passed.")

if __name__ == "__main__":
    main()
