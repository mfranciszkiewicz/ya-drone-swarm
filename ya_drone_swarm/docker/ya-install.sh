#!/bin/bash
set -euo pipefail

mkfifo fifo
exec 3<> fifo

# run requestor (for gftp) and provider installers
variants=("as-requestor" "as-provider")

for i in ${!variants[@]}; do
  variant=${variants[$i]}
  echo "running installer: ${variant}"

  curl -sSf "https://join.golem.network/${variant}" -o "${variant}.sh"
  chmod +x "${variant}.sh"

  sed -i 's/\-u 2/\-u 3/g' "${variant}.sh"
  sed -i 's/"$_src_core\/golemsp"/\/bin\/true/g' "${variant}.sh"

  echo "yes" > fifo
  "./${variant}.sh"
  rm "${variant}.sh"
done

# golemsp setup

echo "node
devnet-beta.2

0.0001" | golemsp setup

# ya-service-bus setup

page=$(curl -sSf "https://github.com/golemfactory/ya-service-bus/releases.atom" \
         | xmlstarlet sel -N a="http://www.w3.org/2005/Atom" \
         -t -v "/a:feed/a:entry[position()=1]/a:link/@href" \
         -n)
uri="https://github.com$(curl -sSf "${page}" | grep -Po '(?i)(/.*ya-sb-router-linux.*\.tar\.gz)' | head -n1)"

curl -sSfL "${uri}" -o ya-sb-router.tar.gz
mkdir ./ya-sb-router
tar xf ya-sb-router.tar.gz -C ./ya-sb-router
mv ./ya-sb-router/*/ya-sb-router /root/.local/bin/

# cleanup

rm -rf ya-sb-router*
rm fifo
