#!/bin/bash

set -e

SPECFILE="varstored.spec"

cd $(dirname $0)/..

TARBALL=$(grep ^Source0 SPECS/${SPECFILE} | awk -F ':' '{print $2}' | awk '{$1=$1;print}')

PEM_FILES=$(tar --auto-compress --list --file SOURCES/${TARBALL} --wildcards "*/certs/*.pem" 2>/dev/null || true)

[ -z "${PEM_FILES}" ] &&
	echo "No pem file found in ${TARBALL}" &&
	exit 1

echo "Removing the following pem files from ${TARBALL}"
echo "${PEM_FILES}"

gunzip SOURCES/${TARBALL}

TARBALL=$(basename ${TARBALL} .gz)

tar --delete --file SOURCES/${TARBALL} ${PEM_FILES}

gzip SOURCES/${TARBALL}

echo "Done"
