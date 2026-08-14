#!/usr/bin/env bash -C -e -u -o pipefail
mosdepth \
    --threads 4 \
     \
    --fasta genome.fasta \
    -n --fast-mode --by 500 \
    test.recal \
    test.recal.cram

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:CRAM_SAMPLEQC:CRAM_QC_RECAL:MOSDEPTH":
    mosdepth: $(mosdepth --version 2>&1 | sed 's/^.*mosdepth //; s/ .*$//')
END_VERSIONS
