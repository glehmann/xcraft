#!/usr/bin/env python3

# Copyright (C) 2020 - Vates
#
# This script is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# It is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this script.  If not, see <http://www.gnu.org/licenses/>.

import sys, os, argparse
from typing import List
from xcp import bootloader

def grub_path(root: str = '/') -> str:
    """
    Find grub file path.

    Keyword arguments:
    root -- root path
    """
    possible_paths = [
        "boot/efi/EFI/xenserver/grub.cfg",
        "boot/grub/grub.cfg",
        "boot/grub2/grub.cfg"
    ]
    for rel_filepath in possible_paths:
        filepath = os.path.join(root, rel_filepath)
        if os.path.exists(filepath):
            return filepath

    raise RuntimeError("No existing bootloader configuration found")

def title_from_flavour_version(flavour: str, version: str) -> str:
    if flavour == 'kernel':
        return "XCP-ng"
    else:
        return "XCP-ng %s %s" % (flavour, version)

def create_menu_entry_from_existing(entry: bootloader.MenuEntry,
                                    version: str, title: str) -> bootloader.MenuEntry:
    return bootloader.MenuEntry(
        entry.hypervisor,
        entry.hypervisor_args,
        f"/boot/vmlinuz-{version}-xen",
        entry.kernel_args,
        f"/boot/initrd-{version}-xen.img",
        title,
        root = entry.root
    )

def add(grub_file: str, flavour: str, version: str, ignore_existing: bool = False) -> None:
    """
    Add new grub entry by copying "XCP-ng" entry into "XCP-ng {flavour}"
    such that the additional kernel can be selected.

    Keyword arguments:
    grub_file -- the file path to write grub configuration
    flavour -- one of XCP-ng's kernel flavours: kernel-alt...
               Value `kernel` is forbidden for this function.
    version -- the kernel version e.g. 4.19.102
    ignore_existing -- if True and no kernel is found for the given parameters, stop silently
    """
    if flavour == 'kernel':
        print("'%s' is a forbidden value for this action." % flavour, file=sys.stderr)
        sys.exit(1)

    vmlinuz = '/boot/vmlinuz-%s' % version
    if not os.path.exists(vmlinuz):
        print("kernel %s doesn't exist." % vmlinuz, file=sys.stderr)
        sys.exit(1)

    b = bootloader.Bootloader("grub2", grub_file)
    b = b.loadExisting()

    title = title_from_flavour_version(flavour, version)
    # If the entry is already present, stop
    for key in b.menu:
        if b.menu.get(key).title == title:
            if ignore_existing:
                return
            else:
                print("%s is already present in grub configuration." % title,
                      file=sys.stderr)
                sys.exit(1)

    # If version is not present, use default kernel as template
    new_entry = create_menu_entry_from_existing(b.menu.get('xe'), version, title)
    b.append("new", new_entry)
    print(f"Adding '{title}' as grub entry #{str(len(b.menu)-1)}")
    b.writeGrub2(grub_file)

def remove(grub_file: str, flavour: str, version: str, ignore_missing: bool = False) -> None:
    """
    Remove the grub boot entry related to the kernel flavour and version

    Keyword arguments:
    grub_file -- the file path to write grub configuration
    flavour -- one of XCP-ng's kernel flavours: kernel-alt...
               Value 'kernel' is forbidden for this function.
    version -- the kernel version e.g. 4.19.102.
    ignore_missing -- if True and no grub entry is found for the given parameters, stop silently
    """
    if flavour == 'kernel':
        print("'%s' is a forbidden value for this action." % flavour, file=sys.stderr)
        sys.exit(1)

    b = bootloader.Bootloader("grub2", grub_file)
    b = b.loadExisting()

    title = title_from_flavour_version(flavour, version)
    for key in b.menu:
        if b.menu.get(key).title == title:
            if b.default == key:
                print("Setting back main kernel as default grub entry.")
                b.default = 'xe'
            print(f"Removing '{title}' from grub entries")
            b.remove(key)
            b.writeGrub2(grub_file)
            return

    if not ignore_missing:
        print("Entry '%s' not found in grub configuration, can't remove it." % title,
              file=sys.stderr)
        sys.exit(1)

def replace(grub_file: str, flavour: str, old_version: str, version: str, ignore_missing: bool = False) -> None:
    """
    Replace an existing grub entry with a new one

    Keyword arguments:
    grub_file -- the file path to write grub configuration
    flavour -- one of XCP-ng's kernel flavours: kernel-alt...
               Value 'kernel' is forbidden for this function.
    old_version -- the kernel version of the entry to replace.
    version -- the kernel version e.g. 4.19.102.
    ignore_missing -- if True and no grub entry is found for the given flavour + old_version,
                      gracefully fall back to the add action
    """
    if flavour == 'kernel':
        print("'%s' is a forbidden value for this action." % flavour, file=sys.stderr)
        sys.exit(1)

    if old_version == version:
        print("I can't replace version %s with itself." % old_version)
        sys.exit(1)

    vmlinuz = '/boot/vmlinuz-%s' % version
    if not os.path.exists(vmlinuz):
        print("kernel %s doesn't exist." % vmlinuz, file=sys.stderr)
        sys.exit(1)

    b = bootloader.Bootloader("grub2", grub_file)
    b = b.loadExisting()

    old_title = title_from_flavour_version(flavour, old_version)
    title = title_from_flavour_version(flavour, version)
    for key in b.menu:
        if b.menu.get(key).title == old_title:
            is_default = b.default == key
            print(f"Replacing '{old_title}' grub entry with '{title}'.")
            # create the new menu entry from the existing for the same kernel flavour
            new_entry = create_menu_entry_from_existing(b.menu.get(key), version, title)
            b.append("new", new_entry)
            if is_default:
                print("As the former was the default entry, setting the new one as default.")
                b.default = "new"
            b.remove(key)
            b.writeGrub2(grub_file)
            return

    if not ignore_missing:
        print("Entry '%s' not found in grub configuration, can't replace it." % old_title,
              file=sys.stderr)
        sys.exit(1)
    else:
        # treat the situation as a simple add
        add(grub_file, flavour, version)


def set_default(grub_file: str, flavour: str, version: str) -> None:
    """
    Set an existing grub boot entry as default

    Keyword arguments:
    grub_file -- the file path to write grub configuration
    flavour -- one of XCP-ng's kernel flavours: kernel, kernel-alt...
    version -- the kernel version e.g. 4.19.102. Value ignored if flavour is 'kernel'.
    """
    b = bootloader.Bootloader("grub2", grub_file)
    b = b.loadExisting()

    title = title_from_flavour_version(flavour, version)
    for key in b.menu:
        if b.menu.get(key).title == title:
            if b.default == key:
                print("'%s' is already the default." % title)
                return
            print("Setting '%s' as default grub entry." % title)
            b.default = key
            b.writeGrub2(grub_file)
            return

    print("Entry '%s' not found in grub configuration, can't set it as default." % title,
          file=sys.stderr)
    sys.exit(1)


def main(argv: List[str]) -> None:
    grub_file = grub_path()
    parser = argparse.ArgumentParser(description='Update grub menu entries for XCP-ng\'s additional kernels')
    actions = ['add', 'remove', 'replace', 'set-default']
    parser.add_argument('action', choices=actions,
                        help="An action among: %s." % ', '.join(actions))
    flavours = ['kernel', 'kernel-alt']
    parser.add_argument('flavour', choices=flavours,
                        help="Kernel flavour among: %s." % ', '.join(flavours))
    parser.add_argument('version',
                        help="version of the selected kernel flavour.\n"
                             "For the `kernel` flavour, the actual value is ignored. You can put '' for example.")
    parser.add_argument('--ignore-existing', action='store_true',
                        help="in the case of the add action, stop silently if the entry is already present")
    parser.add_argument('--ignore-missing', action='store_true',
                        help="in the case of the remove action, stop silently if the entry is not found\n"
                             "and in the case of the replace action, silently convert the action into a add")
    parser.add_argument('--old-version', help="in the case of the replace action, the version to be replaced")
    args = parser.parse_args()

    if args.action == 'add':
        add(grub_file, args.flavour, args.version, ignore_existing=args.ignore_existing)
    elif args.action == 'remove':
        remove(grub_file, args.flavour, args.version, ignore_missing=args.ignore_missing)
    elif args.action == 'replace':
        if not args.old_version:
            print("The --old-version parameter is required with the replace action.")
            sys.exit(1)
        replace(grub_file, args.flavour, args.old_version, args.version, args.ignore_missing)
    elif args.action == 'set-default':
        set_default(grub_file, args.flavour, args.version)

if __name__ == "__main__":
   main(sys.argv[1:])
