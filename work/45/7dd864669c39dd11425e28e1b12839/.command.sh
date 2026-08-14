#!/usr/bin/env bash -C -e -u -o pipefail
grep -v '^@' genome.interval_list | awk -vFS="	" '{
    name = sprintf("%s_%d-%d", $1, $2, $3);
    printf("%s\t%d\t%d\n", $1, $2-1, $3) > name ".bed"
}'

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:PREPARE_INTERVALS:CREATE_INTERVALS_BED":
    gawk: $(awk -Wversion | sed '1!d; s/.*Awk //; s/,.*//')
END_VERSIONS
