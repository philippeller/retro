#for file in {17..17}
#for file in {10..19}
for file in {20..25}
do
for dir in {0..59}
do
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A cyberlamp \
#-l qos=cl_open \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A dfc13_b_g_sc_default \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A open \
#-l qos=cl_open \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A cyberlamp \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A dfc13_a_g_sc_default \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A cyberlamp -l qos=cl_open \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A open \
#    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A cyberlamp \
    echo "/storage/home/pde3/retro/scripts/reco_file.sh $dir $file" | qsub -A dfc13_b_g_lc_default \
-l nodes=1:ppn=1 \
-l pmem=8000mb \
-l walltime=24:00:00 \
-N r$dir.$file \
-o /gpfs/scratch/pde3/retro/log/$dir.$file.log \
-e /gpfs/scratch/pde3/retro/log/$dir.$file.err
done
done
