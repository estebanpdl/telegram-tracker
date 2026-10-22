# -*- coding: utf-8 -*-
'''
Check that this process can, or cannot, open TCP connections. `open`
expects every probe to connect (the control, run before the block);
`blocked` expects every probe to fail (run under the same block as the
tests, by the same interpreter). Probes are a host name and a literal
address of general services, never Telegram's; a connection is closed at
once without sending data. Exits 1 when the expectation fails.

	python network_probe.py open|blocked
'''

# import modules
import socket
import sys


PROBES = (('github.com', 443), ('1.1.1.1', 443))
TIMEOUT = 10


def connects(host, port):
	try:
		with socket.create_connection((host, port), timeout=TIMEOUT):
			return True, 'connected'
	except OSError as exc:
		return False, f'{type(exc).__name__}: {exc}'


def pair_works():
	'''
	asyncio's event loop needs socket.socketpair() (a loopback TCP pair on
	Windows), so the block must leave it working.
	'''
	try:
		first, second = socket.socketpair()
		with first, second:
			first.sendall(b'x')
			return second.recv(1) == b'x'
	except OSError:
		return False


def main(expect):
	if expect not in ('open', 'blocked'):
		sys.exit('usage: network_probe.py open|blocked')
	failures = 0
	paired = pair_works()
	failures += not paired
	print(f'{"ok" if paired else "UNEXPECTED"}: socketpair {"works" if paired else "fails"}')
	for host, port in PROBES:
		connected, detail = connects(host, port)
		ok = connected == (expect == 'open')
		failures += not ok
		print(f'{"ok" if ok else "UNEXPECTED"}: {host}:{port} {detail} (expected {expect})')
	return 1 if failures else 0


if __name__ == '__main__':
	sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ''))
