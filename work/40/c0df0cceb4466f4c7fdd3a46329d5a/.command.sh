#!/usr/bin/env bash -C -e -u -o pipefail
bcftools stats \
     \
     \
     \
     \
     \
     \
    test.strelka.variants.vcf.gz > test.strelka.variants.bcftools_stats.txt

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:VCF_QC_BCFTOOLS_VCFTOOLS:BCFTOOLS_STATS":
    bcftools: $(bcftools --version 2>&1 | head -n1 | sed 's/^.*bcftools //; s/ .*$//')
END_VERSIONS
