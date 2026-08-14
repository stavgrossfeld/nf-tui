#!/usr/bin/env bash -C -e -u -o pipefail
gatk --java-options "-Xmx3276M -XX:-UsePerfData" \
    BaseRecalibrator  \
    --input test.md.cram \
    --output test.recal.table \
    --reference genome.fasta \
    --intervals chr22_1-40001.bed \
    --known-sites dbsnp_146.hg38.vcf.gz --known-sites mills_and_1000G.indels.vcf.gz \
    --tmp-dir . \


cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:FASTQ_PREPROCESS_GATK:BAM_BASERECALIBRATOR:GATK4_BASERECALIBRATOR":
    gatk4: $(echo $(gatk --version 2>&1) | sed 's/^.*(GATK) v//; s/ .*$//')
END_VERSIONS
