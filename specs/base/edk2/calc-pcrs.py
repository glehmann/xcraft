#
# This program calculates and prints out PCR0 and PCR2 from build artifacts
# according to OVMF behaviour.
#

import binascii
import hashlib
import sys

def hash_and_extend(pcr, data):
    m = hashlib.sha256()
    m.update(data)
    digest = m.digest()

    m = hashlib.sha256()
    m.update(pcr)
    m.update(digest)
    return m.digest()


def calc_pcr_0():
    scrtm_version = b'\x00\x00'
    separator = b'\x00\x00\x00\x00'

    with open(sys.argv[1], 'rb') as f:
        pei_fv = f.read()

    with open(sys.argv[2], 'rb') as f:
        dxe_fv = f.read()

    # PCR0 starts empty.
    pcr = b'\x00' * 32

    # PCR0 - event EV_S_CRTM_VERSION
    # The version is always unset (all zeros)
    pcr = hash_and_extend(pcr, scrtm_version)

    # PCR0 - event EV_EFI_PLATFORM_FIRMWARE_BLOB
    # This is the PEI FV blob.
    pcr = hash_and_extend(pcr, pei_fv)

    # PCR0 - event EV_EFI_PLATFORM_FIRMWARE_BLOB
    # This is the DXE FV blob.
    pcr = hash_and_extend(pcr, dxe_fv)

    # PCR0 - event EV_SEPARATOR
    # The separator is all zeros and is logged when
    # calling the EFI application.
    pcr = hash_and_extend(pcr, separator)

    print('PCR0 SHA256', binascii.hexlify(pcr).decode())


def calc_pcr_2():
    separator = b'\x00\x00\x00\x00'

    # PCR2 starts empty.
    pcr = b'\x00' * 32

    # PCR0 - event EV_SEPARATOR
    # The separator is all zeros and is logged when
    # calling the EFI application.
    pcr = hash_and_extend(pcr, separator)

    print('PCR2 SHA256', binascii.hexlify(pcr).decode())


if __name__ == "__main__":
    calc_pcr_0()
    calc_pcr_2()
