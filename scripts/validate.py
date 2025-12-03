#!/usr/bin/env python3
import json
import os
import sys
import re
import glob
import subprocess
import tempfile
import urllib.request
import shutil

# Constants for validation
REQUIRED_TOP_LEVEL = {"plugin_id", "versions", "public_keys"}
REQUIRED_VERSION_LEVEL = {"version", "artifacts"}
REQUIRED_ARTIFACT_LEVEL = {"os", "arch", "url", "sha256", "minisign"}
VALID_OS = {"macos"}
VALID_ARCH = {"arm64"}

# SemVer regex (simplified but robust enough for basic validation)
# Matches 1.0.0, 1.0.0-beta, 1.0.0+build, etc.
SEMVER_REGEX = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")

# Minisign public key format: starts with "RW" and is base64 encoded (typically 56 chars)
MINISIGN_PUBKEY_REGEX = re.compile(r"^RW[A-Za-z0-9+/]{50,}={0,2}$")

def validate_semver(version):
    return bool(SEMVER_REGEX.match(version))

def validate_public_keys(public_keys, context):
    """Validate public_keys object contains valid minisign public key."""
    if not isinstance(public_keys, dict):
        print(f"Error in {context}: 'public_keys' must be a dictionary")
        return False
    
    if "minisign" not in public_keys:
        print(f"Error in {context}: 'public_keys' must contain 'minisign' key")
        return False
    
    pubkey = public_keys["minisign"]
    if not isinstance(pubkey, str):
        print(f"Error in {context}: 'public_keys.minisign' must be a string")
        return False
    
    if not MINISIGN_PUBKEY_REGEX.match(pubkey):
        print(f"Error in {context}: Invalid minisign public key format. Must start with 'RW' and be base64 encoded")
        return False
    
    return True

def validate_minisign_signature(minisign_obj, context):
    """Validate minisign signature object in artifact."""
    if not isinstance(minisign_obj, dict):
        print(f"Error in {context}: 'minisign' must be a dictionary")
        return False
    
    if "signature" not in minisign_obj:
        print(f"Error in {context}: 'minisign' must contain 'signature' field")
        return False
    
    signature = minisign_obj["signature"]
    if not isinstance(signature, str):
        print(f"Error in {context}: 'minisign.signature' must be a string")
        return False
    
    # Minisign signature format has specific structure:
    # - Line 1: "untrusted comment: ..."
    # - Line 2: Base64 signature (starts with key algorithm prefix, e.g., "RW")
    # - Line 3: "trusted comment: ..."
    # - Line 4: Base64 global signature
    lines = signature.strip().split('\n')
    if len(lines) != 4:
        print(f"Error in {context}: Invalid minisign signature format (expected 4 lines, got {len(lines)})")
        return False
    
    if not lines[0].startswith("untrusted comment:"):
        print(f"Error in {context}: Minisign signature must start with 'untrusted comment:'")
        return False
    
    if not lines[2].startswith("trusted comment:"):
        print(f"Error in {context}: Minisign signature line 3 must start with 'trusted comment:'")
        return False
    
    # Verify line 2 and 4 are base64 (signature data)
    base64_pattern = re.compile(r"^[A-Za-z0-9+/]+=*$")
    if not base64_pattern.match(lines[1]):
        print(f"Error in {context}: Invalid base64 in minisign signature line 2")
        return False
    
    if not base64_pattern.match(lines[3]):
        print(f"Error in {context}: Invalid base64 in minisign signature line 4")
        return False
    
    return True

def check_public_key_immutability(filepath, current_public_key):
    """
    Check that public key hasn't changed from base branch.
    Only runs in PR context (when GITHUB_BASE_REF is set).
    Returns True if check passes or is not applicable.
    """
    base_ref = os.environ.get("GITHUB_BASE_REF", "")
    if not base_ref:
        # Not in PR context, skip immutability check
        return True
    
    # Get relative path for git
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    rel_path = os.path.relpath(filepath, repo_root)
    
    try:
        # Try to get the file from base branch
        result = subprocess.run(
            ["git", "show", f"origin/{base_ref}:{rel_path}"],
            capture_output=True,
            text=True,
            cwd=repo_root
        )
        
        if result.returncode != 0:
            # File doesn't exist on base branch - this is a new plugin, allow any public key
            print(f"  New plugin detected (not on {base_ref}), public key registration allowed")
            return True
        
        # Parse the base branch version
        base_data = json.loads(result.stdout)
        base_public_key = base_data.get("public_keys", {}).get("minisign", "")
        
        if not base_public_key:
            # No public key on base branch (shouldn't happen with new requirements)
            print(f"  No public key found on {base_ref}, allowing initial registration")
            return True
        
        if current_public_key != base_public_key:
            print(f"Error: Public key modification detected!")
            print(f"  Base branch ({base_ref}) key: {base_public_key}")
            print(f"  Current key: {current_public_key}")
            print(f"  Public keys are IMMUTABLE after initial registration.")
            print(f"  Only the original author (holder of the private key) can sign updates.")
            return False
        
        print(f"  Public key unchanged from {base_ref} (immutability check passed)")
        return True
        
    except json.JSONDecodeError:
        print(f"  Warning: Could not parse base branch version of {filepath}")
        return True
    except Exception as e:
        print(f"  Warning: Could not check public key immutability: {e}")
        return True

def verify_artifact_signature(artifact, public_key, context):
    """Download artifact and verify minisign signature against public key."""
    url = artifact["url"]
    signature = artifact["minisign"]["signature"]
    
    # Create temporary directory for verification
    tmpdir = tempfile.mkdtemp(prefix="osaurus_verify_")
    try:
        # Download artifact
        artifact_path = os.path.join(tmpdir, "artifact.zip")
        print(f"  Downloading {url}...")
        try:
            urllib.request.urlretrieve(url, artifact_path)
        except Exception as e:
            print(f"  Warning in {context}: Could not download artifact for signature verification: {e}")
            print(f"  Skipping signature verification (artifact unreachable)")
            return True  # Don't fail on unreachable artifacts (might be new release not yet published)
        
        # Write public key file (minisign format requires specific format)
        pubkey_path = os.path.join(tmpdir, "minisign.pub")
        with open(pubkey_path, 'w') as f:
            f.write(f"untrusted comment: minisign public key\n{public_key}\n")
        
        # Write signature file
        sig_path = os.path.join(tmpdir, "artifact.zip.minisig")
        with open(sig_path, 'w') as f:
            f.write(signature)
        
        # Run minisign verification
        result = subprocess.run(
            ["minisign", "-V", "-p", pubkey_path, "-m", artifact_path, "-x", sig_path],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            print(f"Error in {context}: Minisign signature verification FAILED")
            print(f"  minisign output: {result.stderr.strip()}")
            print(f"  This artifact was NOT signed by the registered public key!")
            return False
        
        print(f"  Signature verified for {os.path.basename(url)}")
        return True
        
    finally:
        # Clean up temporary directory
        shutil.rmtree(tmpdir, ignore_errors=True)

def validate_artifact(artifact, context):
    missing = REQUIRED_ARTIFACT_LEVEL - set(artifact.keys())
    if missing:
        print(f"Error in {context}: Missing required artifact fields: {missing}")
        return False
    
    if artifact["os"] not in VALID_OS:
        print(f"Error in {context}: Invalid OS '{artifact['os']}'. Must be one of {VALID_OS}")
        return False
        
    if artifact["arch"] not in VALID_ARCH:
        print(f"Error in {context}: Invalid Architecture '{artifact['arch']}'. Must be one of {VALID_ARCH}")
        return False

    # URL Check: Verify urls are HTTPS
    if not artifact["url"].startswith("https://"):
        print(f"Error in {context}: URL must start with https://")
        return False
        
    if not re.match(r"^[a-fA-F0-9]{64}$", artifact["sha256"]):
        print(f"Error in {context}: Invalid SHA256 checksum format")
        return False
    
    # Validate minisign signature object
    if not validate_minisign_signature(artifact["minisign"], f"{context} -> minisign"):
        return False
        
    return True

def validate_version(entry, context, public_key=None, verify_signatures=False):
    missing = REQUIRED_VERSION_LEVEL - set(entry.keys())
    if missing:
        print(f"Error in {context}: Missing required version fields: {missing}")
        return False
        
    if not validate_semver(entry["version"]):
        print(f"Error in {context}: Invalid Semantic Version '{entry['version']}'")
        return False
        
    if not isinstance(entry["artifacts"], list) or not entry["artifacts"]:
        print(f"Error in {context}: 'artifacts' must be a non-empty list")
        return False

    valid = True
    has_macos_arm64 = False
    
    for idx, artifact in enumerate(entry["artifacts"]):
        artifact_context = f"{context} -> artifact[{idx}]"
        if not validate_artifact(artifact, artifact_context):
            valid = False
        else:
            if artifact["os"] == "macos" and artifact["arch"] == "arm64":
                has_macos_arm64 = True
            
            # Verify signature if enabled and public key available
            if verify_signatures and public_key:
                if not verify_artifact_signature(artifact, public_key, artifact_context):
                    valid = False
            
    # Artifact Check: Ensure at least one macos/arm64 artifact exists per version
    if not has_macos_arm64:
        print(f"Error in {context}: Must contain at least one artifact for macos/arm64")
        valid = False
            
    # Check requirements if present
    if "requires" in entry:
        reqs = entry["requires"]
        if "osaurus_min_version" in reqs:
            if not validate_semver(reqs["osaurus_min_version"]):
                 print(f"Error in {context}: Invalid osaurus_min_version '{reqs['osaurus_min_version']}'")
                 valid = False
                 
    return valid

def validate_plugin_file(filepath, seen_ids):
    print(f"Validating {filepath}...")
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {filepath}: {e}")
        return False
        
    missing = REQUIRED_TOP_LEVEL - set(data.keys())
    if missing:
        print(f"Error: Missing required top-level fields in {filepath}: {missing}")
        return False
    
    plugin_id = data.get("plugin_id", "")
    
    # Consistency Check: Ensure plugin_id matches the filename
    filename = os.path.basename(filepath)
    if not filename.endswith(".json"):
        print(f"Error: File {filepath} must end in .json")
        return False
        
    expected_id = filename[:-5] # remove .json
    if plugin_id != expected_id:
        print(f"Error: plugin_id '{plugin_id}' does not match filename '{filename}' (expected '{expected_id}')")
        return False
        
    if plugin_id != plugin_id.lower():
        print(f"Error: plugin_id '{plugin_id}' must be lower-case")
        return False

    if not re.match(r"^[a-z0-9]+(\.[a-z0-9_-]+)+$", plugin_id):
        print(f"Error: plugin_id '{plugin_id}' must be in dot-separated format (e.g., osaurus.time, osaurus.macos-use)")
        return False

    # Unique Constraint: Ensure no duplicate plugin_ids (case-insensitive)
    lower_id = plugin_id.lower()
    if lower_id in seen_ids:
        print(f"Error: Duplicate plugin_id found: '{plugin_id}'. Another file already uses this ID (case-insensitive match).")
        return False
    seen_ids.add(lower_id)

    if not isinstance(data["versions"], list):
        print(f"Error: 'versions' must be a list in {filepath}")
        return False

    # Validate public_keys (required even for unreleased plugins)
    if not validate_public_keys(data["public_keys"], filepath):
        return False
    
    public_key = data["public_keys"]["minisign"]
    
    # Check public key immutability (only in PR context)
    if not check_public_key_immutability(filepath, public_key):
        return False

    # Empty versions array is allowed for unreleased plugins
    if len(data["versions"]) == 0:
        print(f"  Note: No versions published yet for {plugin_id}")
        return True

    # Check if signature verification is enabled (via environment variable)
    verify_signatures = os.environ.get("VERIFY_SIGNATURES", "").lower() in ("1", "true", "yes")
    
    if verify_signatures:
        # Check if minisign is available
        if shutil.which("minisign") is None:
            print("Error: VERIFY_SIGNATURES is enabled but minisign is not installed")
            return False
        print(f"  Signature verification enabled for {plugin_id}")

    valid = True
    for idx, version_entry in enumerate(data["versions"]):
        if not validate_version(version_entry, f"{filepath} -> version[{idx}]", public_key, verify_signatures):
            valid = False
            
    return valid

def main():
    # Look for plugins directory relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    plugins_dir = os.path.join(os.path.dirname(script_dir), "plugins")
    
    if not os.path.isdir(plugins_dir):
        print(f"Error: plugins directory not found at {plugins_dir}")
        sys.exit(1)

    json_files = glob.glob(os.path.join(plugins_dir, "*.json"))
    
    if not json_files:
        print("No plugin files found to validate.")
        # Not an error, just empty repo check
        return 0 

    failed = False
    seen_ids = set()
    
    for json_file in json_files:
        if not validate_plugin_file(json_file, seen_ids):
            failed = True
            
    if failed:
        print("\nValidation FAILED.")
        sys.exit(1)
    else:
        print("\nAll plugins validated successfully.")
        sys.exit(0)

if __name__ == "__main__":
    main()

