import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .utils import make_request
from core.utils import sanitize_filename # Corrected import
import copy # Added import for deepcopy

# Footprints for CAPTCHA plugins (can be shared or expanded from auth_hardening_checker)
CAPTCHA_FOOTPRINTS_REG = {
    "Google reCAPTCHA": [re.compile(r"google.com/recaptcha|grecaptcha", re.IGNORECASE)],
    "hCaptcha": [re.compile(r"hcaptcha.com|h-captcha", re.IGNORECASE)],
    "Cloudflare Turnstile": [re.compile(r"challenges.cloudflare.com/turnstile|cf-turnstile", re.IGNORECASE)],
    "Really Simple CAPTCHA": [re.compile(r"really-simple-captcha", re.IGNORECASE)],
}

def _check_reg_form_protections(html_content, reg_form_details):
    """Analyzes registration form HTML for CAPTCHA and password strength hints."""
    if not html_content:
        return

    soup = BeautifulSoup(html_content, 'html.parser')
    form_html_lower = html_content.lower() # Analyze full page for some hints

    # CAPTCHA Detection
    detected_captchas = []
    for name, patterns in CAPTCHA_FOOTPRINTS_REG.items():
        for pattern in patterns:
            if pattern.search(html_content): # Search raw HTML
                if name not in detected_captchas:
                    detected_captchas.append(name)
                break
    if detected_captchas:
        reg_form_details["captcha_detected"] = detected_captchas
        print(f"        [+] CAPTCHA detected on registration form: {', '.join(detected_captchas)}")
    else:
        reg_form_details["captcha_detected"] = False
        print("        [-] No common CAPTCHA footprints detected on registration form.")

    # Password Strength Meter / Policy Hints (Passive)
    password_fields = soup.find_all('input', attrs={'type': 'password'})
    # Common names for registration password fields: user_pass, pass1, pass1-text, password, user_password
    reg_password_field = None
    for pf in password_fields:
        pf_name = pf.get('name', '').lower()
        if any(n in pf_name for n in ['pass1', 'user_pass', 'password_current', 'new_password']): # pass1 for wp-signup
            reg_password_field = pf
            break
    
    if reg_password_field:
        reg_form_details["password_policy_hints"] = {}
        if reg_password_field.get('minlength'):
            reg_form_details["password_policy_hints"]["minlength"] = reg_password_field['minlength']
            print(f"        [i] Password field has minlength: {reg_password_field['minlength']}")
        if reg_password_field.get('pattern'):
            reg_form_details["password_policy_hints"]["pattern"] = reg_password_field['pattern']
            print(f"        [i] Password field has pattern: {reg_password_field['pattern']}")
        # Look for JS-based strength meter indicators (e.g., a div with class 'password-strength-meter')
        strength_meter_div = soup.find(id=re.compile(r"pass-strength-result|password-strength-meter", re.I)) or \
                             soup.find('div', class_=re.compile(r"password-strength|strength-meter", re.I))
        if strength_meter_div:
            reg_form_details["password_policy_hints"]["strength_meter_present"] = True
            print("        [i] Password strength meter indicator found.")
    
    # Email Verification Hint (from text on page)
    email_verif_keywords = ["confirmation email", "verify your email", "activation link", "check your email to activate"]
    if any(kw in form_html_lower for kw in email_verif_keywords):
        reg_form_details["email_verification_hint"] = True
        print("        [i] Text suggests email verification might be required for new registrations.")
    else:
        reg_form_details["email_verification_hint"] = False


def analyze_user_registration(state, config, target_url):
    """Analyzes user registration status, paths, and form security aspects."""
    wp_analyzer_module_key = "wp_analyzer"
    findings_subkey = "user_registration" # Align with analyzer.py

    _DEFAULT_USER_REG_FINDINGS = {
        "details_summary": "Analyzing user registration process...",
        "registration_enabled": None, 
        "registration_url_found": None,
        "registration_form_details": {
            "captcha_detected": False,
            "password_policy_hints": {},
            "email_verification_hint": None,
            "allows_user_registration_hint": None,
            "allows_site_registration_hint": None
        },
        "default_role_check_status": "Manual check recommended (cannot determine default role remotely without registration)."
    }

    analyzer_findings_raw = state.get_module_findings(wp_analyzer_module_key, {})
    if not isinstance(analyzer_findings_raw, dict):
        analyzer_findings = {}
        # Optionally, log this state corruption:
        # state.add_tool_error(f"Warning: Findings for module '{wp_analyzer_module_key}' were not a dictionary. Resetting for '{findings_subkey}'.")
    else:
        analyzer_findings = analyzer_findings_raw
    
    existing_phase_findings = analyzer_findings.get(findings_subkey, {})

    # Start with a deep copy of the default structure
    current_phase_findings = copy.deepcopy(_DEFAULT_USER_REG_FINDINGS)

    # Update with existing findings if they are a dictionary
    if isinstance(existing_phase_findings, dict):
        for key, value in existing_phase_findings.items():
            if key == "registration_form_details":
                # Only update the nested dict if the 'value' from existing_phase_findings is also a dict.
                # This prevents overwriting the default dict with a non-dict value.
                if isinstance(value, dict) and isinstance(current_phase_findings.get(key), dict):
                    current_phase_findings[key].update(value)
                # else: existing 'registration_form_details' is not a dict, so we keep the default dict structure.
                #      Optionally, one could log a warning here.
            elif key in current_phase_findings: # For other keys, update if the key is part of the default structure.
                current_phase_findings[key] = value
            # Keys from existing_phase_findings not in _DEFAULT_USER_REG_FINDINGS are ignored to maintain a clean structure.

    # Always set/overwrite status for the current run
    current_phase_findings["status"] = "Running"
    
    # Ensure the findings structure is placed into the parent analyzer_findings
    analyzer_findings[findings_subkey] = current_phase_findings
    
    print("    [i] Analyzing User Registration Security...")

    # Common registration paths - wp-signup.php is especially relevant for multisite
    # Order matters: check more specific/common ones first.
    reg_paths_to_check = [
        urljoin(target_url, "/wp-login.php?action=register"),
        urljoin(target_url, "/wp-signup.php"), # Key for multisite, but can exist on single if plugin enables
        urljoin(target_url, "/register") # Common custom path
    ]

    registration_page_html = None
    
    for test_url in reg_paths_to_check:
        print(f"      Checking potential registration page: {test_url}")
        try:
            response = make_request(test_url, config, method="GET", timeout=10)
            if response and response.status_code == 200:
                text_content = response.text
                text_lower = text_content.lower()
                
                # Keywords indicating a registration page (not just a link to one)
                is_reg_page_keywords = any(kw in text_lower for kw in [
                    "create an account", "registration form", "complete signup", 
                    "choose a username", "get your own site", "reserve a site name" # Multisite specific
                ])
                # Form elements indicating a registration form
                has_reg_form_elements = (
                    re.search(r'<form[^>]+(?:id=["\']registerform["\']|name=["\']registerform["\']|id=["\']setupform["\'])', text_content, re.I) or
                    (re.search(r'input[^>]+name=["\']user_login["\']', text_content, re.I) and
                     re.search(r'input[^>]+name=["\']user_email["\']', text_content, re.I))
                )
                # Avoid matching login forms that might contain "register" links
                is_just_login_form = ("log in" in text_lower and "user_pass" in text_lower and not is_reg_page_keywords)

                if is_reg_page_keywords and has_reg_form_elements and not is_just_login_form:
                    # Check for messages like "User registration is currently not allowed." or "Registration has been disabled."
                    if "registration is currently not allowed" in text_lower or "registration has been disabled" in text_lower:
                        current_phase_findings["registration_enabled"] = False
                        current_phase_findings["registration_url_found"] = test_url
                        print(f"        [+] Registration page found at {test_url}, but explicitly states registration is disabled.")
                        break 
                    else:
                        current_phase_findings["registration_enabled"] = True
                        current_phase_findings["registration_url_found"] = test_url
                        registration_page_html = text_content
                        print(f"        [!!!] User/Site registration appears to be OPEN at: {test_url}")
                        break 
            elif response: # Non-200 status
                print(f"        [-] Path {test_url} returned status {response.status_code}.")
            # else: request failed, already logged by make_request
        except Exception as e:
            print(f"        [-] Error checking registration path {test_url}: {e}")

    if current_phase_findings["registration_enabled"] is None: # If loop finished without a clear yes/no
        current_phase_findings["registration_enabled"] = "Unknown"
        current_phase_findings["details_summary"] = "Could not definitively determine if user registration is open at common paths."
        print("      [i] Could not definitively determine user registration status from common paths.")

    if registration_page_html: # If we found an accessible registration page HTML
        _check_reg_form_protections(registration_page_html, current_phase_findings["registration_form_details"])
        
        # Specific handling for wp-signup.php (multisite context)
        if current_phase_findings["registration_url_found"] and "wp-signup.php" in current_phase_findings["registration_url_found"]:
            if 'name="user_name"' in registration_page_html.lower() or 'name="user_email"' in registration_page_html.lower():
                current_phase_findings["registration_form_details"]["allows_user_registration_hint"] = True
            if 'name="blogname"' in registration_page_html.lower() or 'name="blog_title"' in registration_page_html.lower():
                current_phase_findings["registration_form_details"]["allows_site_registration_hint"] = True

    # Consolidate details and add remediations
    summary_parts = []
    if current_phase_findings["registration_enabled"] is True:
        summary_parts.append(f"User/Site registration appears OPEN at {current_phase_findings['registration_url_found']}.")
        state.add_critical_alert(f"User/Site registration may be open at {current_phase_findings['registration_url_found']}.")
        
        reg_form_sec = current_phase_findings["registration_form_details"]
        if not reg_form_sec.get("captcha_detected"):
            summary_parts.append("No CAPTCHA detected on registration form.")
            state.add_remediation_suggestion("user_reg_no_captcha", {
                "source": "WP Analyzer (User Registration)",
                "description": "Open user registration form does not appear to have CAPTCHA protection.",
                "severity": "Medium",
                "remediation": "Implement strong CAPTCHA (e.g., reCAPTCHA v3, hCaptcha) on the registration form to prevent spam and bot registrations."
            })
        if not reg_form_sec.get("email_verification_hint"):
            summary_parts.append("No clear indication of email verification for new registrations.")
            # This is informational as it's a passive hint
        
        # Default role check remains manual
        state.add_remediation_suggestion("user_registration_open_v2", { # new key
            "source": "WP Analyzer (User Registration)",
            "description": f"User/Site registration is open at {current_phase_findings['registration_url_found']}. This can be a security risk if not properly managed (spam, abuse, weak default roles).",
            "severity": "Medium",
            "remediation": "If public registration is not required, disable it (Settings > General > Membership for single sites; Network Admin for multisite). If required, ensure new users get the 'Subscriber' role by default, use strong CAPTCHAs, enforce email verification, and monitor new signups."
        })

    elif current_phase_findings["registration_enabled"] is False:
        summary_parts.append(f"User registration appears explicitly disabled (checked at {current_phase_findings.get('registration_url_found', 'common paths')}).")
    else: # Unknown
        summary_parts.append("User registration status at common paths is undetermined.")

    current_phase_findings["details_summary"] = " ".join(summary_parts) if summary_parts else "User registration checks performed."
    current_phase_findings["status"] = "Completed"
    
    analyzer_findings[findings_subkey] = current_phase_findings
    state.update_module_findings(wp_analyzer_module_key, analyzer_findings)
    
    print(f"    [+] User Registration analysis finished. Status: {current_phase_findings['registration_enabled']}. Summary: {current_phase_findings['details_summary']}")
