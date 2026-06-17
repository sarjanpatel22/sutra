"""Tests for the engagement scope guard — only authorized targets run hands-free."""

from friday.core.scope import command_in_scope, extract_hosts, parse_scope


def test_parse_scope_ips_cidrs_hosts():
    s = parse_scope("10.10.10.5, 192.168.1.0/24, target.thm")
    assert s is not None
    assert command_in_scope("nmap 10.10.10.5", s)            # exact IP
    assert command_in_scope("nmap 192.168.1.77", s)          # inside the CIDR
    assert command_in_scope("curl http://target.thm/x", s)   # named host
    assert not command_in_scope("nmap 8.8.8.8", s)           # outside


def test_parse_scope_empty_is_none():
    assert parse_scope("") is None
    assert parse_scope(None) is None
    assert parse_scope("   ") is None


def test_extract_hosts_ip_url_userat():
    h = extract_hosts("ssh root@10.10.10.5 && curl https://evil.com:8080/x ; nmap 1.2.3.4")
    assert h == {"10.10.10.5", "evil.com", "1.2.3.4"}


def test_extract_hosts_ignores_flags_and_bad_ips():
    assert extract_hosts("nmap -sV --max-rate 1000 -p-") == set()
    assert extract_hosts("ping 999.1.1.1") == set()  # invalid octet → not a host


def test_no_network_target_command_is_in_scope():
    s = parse_scope("10.10.10.5")
    assert command_in_scope("whoami", s)
    assert command_in_scope("cat /etc/passwd", s)


def test_loopback_always_in_scope():
    s = parse_scope("10.10.10.5")
    assert command_in_scope("curl http://127.0.0.1:8000", s)
    assert command_in_scope("nc localhost 4444", s)


def test_any_offscope_target_taints_a_compound_command():
    s = parse_scope("10.10.10.5")
    # one in-scope and one out-of-scope host in a single command → the whole thing is off-scope
    assert not command_in_scope("nmap 10.10.10.5 && curl http://evil.com", s)
