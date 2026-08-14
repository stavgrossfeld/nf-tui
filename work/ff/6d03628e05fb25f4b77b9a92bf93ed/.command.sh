#!/usr/bin/env bash -C -e -u -o pipefail
gatk --java-options "-Xmx3276M -XX:-UsePerfData" \
    ApplyBQSR \
    --input test.md.cram \
    --output test.recal.cram \
    --reference genome.fasta \
    --bqsr-recal-file test.recal.table \
    --intervals chr22_1-40001.bed \
    --tmp-dir . \


cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:FASTQ_PREPROCESS_GATK:BAM_APPLYBQSR:GATK4_APPLYBQSR":
    gatk4: $(echo $(gatk --version 2>&1) | sed 's/^.*(GATK) v//; s/ .*$//')
END_VERSIONS
