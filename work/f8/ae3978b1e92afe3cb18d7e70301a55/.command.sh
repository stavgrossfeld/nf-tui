#!/usr/bin/env bash -C -e -u -o pipefail
gatk --java-options "-Xmx12288M -XX:-UsePerfData" \
    MarkDuplicates \
    --INPUT test-test_L1.sorted.bam --INPUT test-test_L2.sorted.bam \
    --OUTPUT test.md.bam \
    --METRICS_FILE test.md.cram.metrics \
    --TMP_DIR . \
    --REFERENCE_SEQUENCE genome.fasta \
    --REMOVE_DUPLICATES false --VALIDATION_STRINGENCY LENIENT

# If cram files are wished as output, the run samtools for conversion
if [[ test.md.cram == *.cram ]]; then
    samtools view -Ch -T genome.fasta -o test.md.cram test.md.bam
    rm test.md.bam
    samtools index test.md.cram
fi

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:FASTQ_PREPROCESS_GATK:BAM_MARKDUPLICATES:GATK4_MARKDUPLICATES":
    gatk4: $(echo $(gatk --version 2>&1) | sed 's/^.*(GATK) v//; s/ .*$//')
    samtools: $(echo $(samtools --version 2>&1) | sed 's/^.*samtools //; s/Using.*$//')
END_VERSIONS
