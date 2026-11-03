# -*- coding: utf-8 -*-
'''
`auth rename-profile OLD NEW` and `auth delete-profile NAME`: offline
maintenance of named profiles (see credentials/maintenance.py). The
operands are explicit: --profile and TELEGRAM_TRACKER_PROFILE are not
used. --dry-run checks and reports without taking a lease or changing
anything.

Exit codes: 0 done, or a dry run that would go ahead; 1 refused,
declined, or failed (part way included); 2 an operand that names no
usable profile, a copied profile folder, or a deletion without a
terminal to confirm it and without --yes. Run through cli.auth.run_auth,
which also maps an interrupt to 130.
'''

# import modules
import os
import sys

# import local submodules
from telegram_tracker.cli.prompts import confirm, interactive
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import maintenance


def fail(message, code):
	print(f'> {message}', file=sys.stderr)
	return code


def dry_run_result(root):
	'''
	Exit code of a dry run: 1, after naming them, when lock files in
	`root` would refuse the real run; 0 otherwise.
	'''
	locks = maintenance.held_locks(root)
	for line in locks:
		print(f'> The command would be refused: {line}.', file=sys.stderr)
	if not locks:
		print('> Dry run: nothing was changed.')
	return 1 if locks else 0


'''

rename-profile

'''

def rename_profile(args, *, client_factory=None, authorize=None):
	home, _ = homes.app_home()
	try:
		plan = maintenance.plan_rename(home, args['old'], args['new'])
		if args['dry_run']:
			verb = 'finish renaming' if plan.finishing else 'rename'
			print(f'> Would {verb} profile {plan.old.name} to {plan.new.name}: {plan.folder} -> {plan.new.root}.')
			return dry_run_result(plan.folder)
		maintenance.rename(plan)
	except maintenance.MaintenanceError as exc:
		return fail(exc, exc.code)

	old, new = plan.old.name, plan.new.name
	done = 'Finished renaming' if plan.finishing else 'Renamed'
	print(
		f'> {done} profile {old} to {new}: {plan.new.root}. Its profile ID, account '
		'binding, saved credentials, and session are unchanged.'
	)
	if plan.finishing and os.path.lexists(plan.old.root):
		print(f'> {plan.old.root} is another profile (another profile ID); it was not changed.')
	print(
		f'> Collected outputs keep the profile name they recorded ({old}). Histories '
		f'recorded with this profile ID continue with --profile {new}; a history recorded '
		f'with the name {old} and no profile ID does not follow the new name.'
	)
	return 0


'''

delete-profile

'''

def delete_profile(args, *, client_factory=None, authorize=None):
	home, _ = homes.app_home()
	try:
		plan = maintenance.plan_delete(home, args['name'])
	except maintenance.MaintenanceError as exc:
		return fail(exc, exc.code)

	name, root = plan.profile.name, plan.profile.root
	print(f'> Profile {name}: {root}')
	print(f'> Removes: {", ".join(plan.files) or "no files"}, then the folder.')
	if plan.identity is None:
		print('> It has no profile.json: what an earlier deletion or an unfinished first login left.')
	print(
		'> Only files on this computer are removed. Collected, derived, and media data '
		f'are not touched. A new profile named {name} later gets a new profile ID.'
	)
	if maintenance.SESSION_FILE in plan.files:
		print(
			'> The session is not logged out on Telegram. To revoke it, run '
			f'`auth logout --profile {name}` first, or end it later under Devices in a '
			'Telegram app.'
		)
	if args['dry_run']:
		return dry_run_result(root)

	if not args['yes']:
		if not interactive():
			return fail(
				'Nothing removed: input is not a terminal, so the deletion cannot be '
				'confirmed. Pass --yes to delete the profile.',
				2
			)
		if not confirm(f'Delete profile {name} from this computer?'):
			print('> Nothing removed.')
			return 1

	try:
		result = maintenance.delete(plan)
	except maintenance.MaintenanceError as exc:
		return fail(exc, exc.code)
	if result.problem:
		return fail(
			f'Profile {name} was deleted only in part: {result.problem}. Removed: '
			f'{", ".join(result.removed) or "nothing"}. Still in {root}: '
			f'{", ".join(result.remaining) or "nothing"}. Run the command again to finish.',
			1
		)
	print(f'> Deleted profile {name}: removed {", ".join(result.removed) or "no files"} and its folder.')
	return 0
