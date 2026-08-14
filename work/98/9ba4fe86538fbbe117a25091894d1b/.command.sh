#!/usr/bin/env bash -C -e -u -o pipefail
samtools \
    stats \
    --threads 1 \
    --reference genome.fasta \
    test.recal.cram \
    > test.recal.cram.stats

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:CRAM_SAMPLEQC:CRAM_QC_RECAL:SAMTOOLS_STATS":
    samtools: $(echo $(samtools --version 2>&1) | sed 's/^.*samtools //; s/Using.*$//')
END_VERSIONS
