#!/usr/bin/env bash -C -e -u -o pipefail
vcftools \
    --gzvcf test.strelka.variants.vcf.gz \
    --out test.strelka.variants \
    --TsTv-by-qual \
     \


cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:VCF_QC_BCFTOOLS_VCFTOOLS:VCFTOOLS_TSTV_QUAL":
    vcftools: $(echo $(vcftools --version 2>&1) | sed 's/^.*VCFtools (//;s/).*//')
END_VERSIONS
