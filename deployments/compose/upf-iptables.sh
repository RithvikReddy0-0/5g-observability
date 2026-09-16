#!/bin/bash

iptables -t nat -A POSTROUTING -s 10.60.0.0/16 ! -o upfgtp -j MASQUERADE
iptables -A FORWARD -i upfgtp -j ACCEPT
iptables -A FORWARD -o upfgtp -j ACCEPT

