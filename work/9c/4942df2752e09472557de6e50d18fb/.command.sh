#!/usr/bin/env bash -C -e -u -o pipefail
samtools \
    index \
    -@ 0 \
     \
    test.recal.cram

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:FASTQ_PREPROCESS_GATK:BAM_APPLYBQSR:CRAM_MERGE_INDEX_SAMTOOLS:INDEX_CRAM":
    samtools: $(echo $(samtools --version 2>&1) | sed 's/^.*samtools //; s/Using.*$//')
END_VERSIONS
