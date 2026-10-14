#!/usr/bin/perl
use strict;
use warnings;
my ($vmid,$phase) = @ARGV;
if ($phase eq 'pre-start') {
    system('/usr/local/sbin/virtualwan-isolation','pre-start','--vmid',$vmid) == 0
        or die "Virtualwan isolation pre-start validation failed\n";
}
